"""mem0 후보 추출 — 언어별 프롬프트와 **인용문 대조**.

기억 재설계(docs/ARCHITECTURE-capi.md 5.2절).

모델은 후보와 그 근거를 낸다. 근거는 **유저 발화에서 그대로 복사한 짧은 구절**이고, 서버가 원문에서
그 구절을 찾아 좌표를 계산한다. 못 찾으면 그 후보만 버린다.

바이트 좌표를 모델에게 세게 하던 방식은 버렸다. 영어는 글자 수 = 바이트 수라 우연히 맞았지만
한국어·일본어는 한 글자가 3바이트라 계속 어긋났다. 좌표를 정확히 대야 한다는 부담이 후보 수도 줄였다.

거부 정책:
 · JSON이 아니거나 `candidates`가 배열이 아니면 → 전량 폐기(모델 출력을 통째로 못 믿는 경우)
 · **후보 하나의 문제는 그 후보만 버린다.** 예전에는 하나만 어긋나도 24개를 다 날리고, 같은 입력으로
   8번 재시도해 잡이 죽었다. 그러면 그 사람의 기억이 통째로 멈춘다.
 · 근거 구절을 원문에서 못 찾으면 그 후보만 폐기

추출 모델은 alias가 아닌 snapshot으로 고정한다(11.3절) — alias가 바뀌면 같은 prompt version에
다른 모델이 조용히 섞인다.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from app.services import i18n, memory
from app.services.mem0_ingest import Candidate, EvidenceSpan

_log = logging.getLogger("moly-worker")

# alias가 아닌 snapshot. 변경은 shadow eval + price catalog version 변경을 거친다.
EXTRACTOR_MODEL = "gpt-4.1-mini-2025-04-14"
# v3: 언어별 프롬프트 전문 분리 · 근거를 인용문으로 · 우선순위 지시 · 부분 폐기(2026-08-08).
# ⚠️ 프롬프트 규칙을 바꾸면 반드시 올린다 — 옛 규칙으로 뽑힌 기억과 구분이 안 되면
#    무엇을 재처리해야 하는지 알 수 없다.
EXTRACTOR_VERSION = "mem0-extractor-v3"
# 출력이 여기서 잘리면 JSON이 안 닫혀 후보를 잃는다. 잘림은 `finish_reason`으로 따로 감지한다.
#
# ⚠️ 이 값은 **반드시 `EXTRACTOR_MODEL`로 실측해서** 정한다. 대화 모델(GPT-5 계열)로 재면
#    추론 토큰이 섞여 몇 배로 나온다 — 그 값으로 정하면 필요 없이 크게 잡는다.
#
# 운영 대화 실측(2026-08-08, gpt-4.1-mini): 20턴 734 / 최대 1,048. 단 그때는 후보가 3개쯤
# 나오던 상태였다. v3는 우선순위 지시로 후보를 8~15개까지 끌어올리고 근거도 인용문(좌표보다 길다)이라
# 출력이 늘어난다. 후보 24개 × 약 120토큰 + 여유로 잡는다.
MAX_OUTPUT_TOKENS = 4_000
# 프롬프트에 적는 후보 개수 상한. 코드 쪽 상한(`mem0_ingest.MAX_CANDIDATES_PER_CHUNK`)과 같은
# 값이어야 한다 — 모델이 더 내면 뒷부분이 조용히 잘린다.
MAX_CANDIDATES = 24

# 허용 category. 목록 밖 값이 와도 **전량 폐기하지 않는다** — 가장 가까운 값으로 흡수하거나
# 그 후보만 버린다. 예전에는 `hobby` 하나가 24개를 다 날렸다.
CATEGORIES: frozenset[str] = frozenset(
    {"preference", "emotion", "relationship", "event", "concern", "routine_intent"}
)
# 목록 밖 값이 왔을 때 대신 쓸 값. 버리는 것보다 낫다 — 본문은 멀쩡한데 라벨만 틀린 경우다.
FALLBACK_CATEGORY = "event"

# 역할 표시는 **언어 중립**으로 둔다. `[유저]`처럼 한국어로 쓰면 비한국어 모델이 그 단어를
# 번역해 출력에 섞는다(`ユuserが...` 사고와 같은 원인).
_ROLE_USER = "[user]"
_ROLE_ASSISTANT = "[assistant]"


class ExtractionSchemaError(ValueError):
    """모델 출력을 통째로 못 믿는다 — 후보 전량 폐기."""


class OutputTruncated(ExtractionSchemaError):
    """출력이 상한에서 잘렸다. 재시도해도 같으므로 덩어리를 쪼개거나 상한을 올려야 한다."""


@dataclass(frozen=True, slots=True)
class SourceMessage:
    """추출에 넘기는 발화 하나.

    ⚠️ `content`는 **살균본**이다. 모델이 보는 글과 근거를 대조하는 글이 같아야 한다 —
    다르면 전각 문자(`ＡＢＣ`·`ｱｲｳ`)나 괄호가 있는 발화에서 좌표가 통째로 어긋난다.
    만드는 쪽(`worker/mem0_jobs.py`)이 `sanitize`로 만들어 넘긴다.
    """

    id: int
    sender: str
    content: str

    @classmethod
    def sanitized(cls, *, id: int, sender: str, content: str | None) -> SourceMessage:
        """원문에서 살균본 발화를 만든다. 이 생성자만 쓰면 두 글이 어긋날 수 없다."""
        return cls(id=id, sender=sender, content=memory.sanitize_text(content or ""))

    @property
    def utf8(self) -> bytes:
        return (self.content or "").encode("utf-8")

    def hash_of(self, start: int, end: int) -> str:
        return hashlib.sha256(self.utf8[start:end]).hexdigest()

    def locate(self, quote: str) -> tuple[int, int] | None:
        """인용 구절의 UTF-8 바이트 구간. 못 찾으면 None.

        모델이 앞뒤 공백을 흘리거나 줄바꿈을 공백으로 바꾸는 일이 잦아 한 번 더 느슨하게 찾는다.
        """
        body = self.content or ""
        q = (quote or "").strip()
        if not q:
            return None
        idx = body.find(q)
        if idx < 0:
            # 공백만 다른 경우를 구제한다. 공백을 지운 사본에서 찾고 원문 좌표로 되돌린다.
            squeezed = "".join(body.split())
            target = "".join(q.split())
            if not target or target not in squeezed:
                return None
            # 원문에서 같은 글자 순서를 다시 훑어 시작·끝 글자 위치를 잡는다.
            hit = squeezed.index(target)
            seen = start_char = 0
            for i, ch in enumerate(body):
                if ch.isspace():
                    continue
                if seen == hit:
                    start_char = i
                    break
                seen += 1
            else:
                return None
            seen = 0
            end_char = start_char
            for i in range(start_char, len(body)):
                if not body[i].isspace():
                    seen += 1
                if seen == len(target):
                    end_char = i + 1
                    break
            else:
                return None
            idx, q = start_char, body[start_char:end_char]
        start = len(body[:idx].encode("utf-8"))
        return start, start + len(q.encode("utf-8"))


# ─────────────────────────────────────────────────────────────
# 언어별 프롬프트 — **전문을 각 언어로 쓴다.**
#
# 예전에는 한국어 본문에 언어 지시 한 줄만 갈아 끼웠다. 그러면 지시문 절반이 한국어라
# 모델이 출력 언어를 한국어로 끌어당기고, `'유저가 ~한다'`라는 한국어 리터럴을 문장 주어로
# 쓰라고 지시하는 꼴이 된다. 일본어 유저 기억이 `ユuserが朝ごはんを...`로 나온 원인이다.
# ─────────────────────────────────────────────────────────────
_OUTPUT_SHAPE = (
    '{"candidates":[{"text":"...","category":"...","evidence":'
    '[{"message_id":123,"quote":"..."}]}]}'
)

_KO = """너는 대화에서 오래 기억할 만한 사실 후보만 뽑는 추출기다. 대화에 답하지 말고 JSON 객체 하나만 출력한다.

[출력]
{shape}

[category] 사실의 종류를 하나 고른다.
- preference: 좋아하는 것·싫어하는 것·취향
- relationship: 사람과의 관계, 그 사람에 대한 감정
- concern: 걱정·고민·부담
- emotion: 그때의 기분
- routine_intent: 반복하는 습관, 하려고 마음먹은 일
- event: 있었던 일
딱 맞는 게 없으면 가장 가까운 것을 고른다.

[무엇을 먼저 뽑는가] 오래 남는 것부터 뽑는다.
1. 취향 — 좋아하는 것, 싫어하는 것, 즐겨 하는 것
2. 사람 — 가족·친구·연인과의 관계와 그 사람에 대한 마음
3. 지속되는 상태 — 하는 일, 사는 곳, 학교, 건강, 오래된 고민
4. 반복하는 습관과 계획
5. 그다음이 이번에 있었던 일

[어떻게 훑는가]
- 대화를 처음부터 끝까지 훑는다. 뒷부분만 보고 끝내지 않는다.
- 한 발화에 오래 남을 사실이 여러 개면 각각 따로 낸다.
- 20턴쯤 되는 대화라면 보통 8~15개가 나온다. 다만 개수를 맞추려고 지어내지는 않는다.

[규칙]
- evidence의 quote는 **유저 발화에서 그대로 복사한 짧은 구절**이다. 한 글자도 바꾸지 않는다.
- 근거를 못 대면 후보로 만들지 않는다. 지어내지 않는다.
- 상대(assistant) 발화는 대명사 해석에만 쓰고 단독 근거로 삼지 않는다.
- 이번 대화에서 새로 드러난 사실만 뽑는다. 추측은 후보가 아니다.
- 정정과 부정도 새 사실이다. 유저가 앞서 말한 걸 뒤집으면('안 했어', '그거 아니야') 그 부정을 후보로 뽑는다. 안 뽑으면 옛 사실이 영원히 남는다.
- 사람 이름·호칭은 쓰지 않는다. {{유저이름}} 같은 토큰이 보이면 **번역하지 말고 글자 그대로** 둔다.
- 지금 착용 중인 것, 잔액, 오늘 루틴 완료 같은 현재 상태는 뽑지 않는다. 정정이나 부정이어도 마찬가지다. 옷·소지품·루틴 목록은 서버에 원본이 있어서, 기억으로 또 들고 있으면 원본과 어긋난 옛 내용이 대화를 이긴다.
- 잊어달라·없던 걸로 해달라는 요청은 후보가 아니다. 지워달라는 말을 기억으로 만들면 그 내용을 오히려 영원히 들고 있게 된다.
- 하려는 것과 한 것을 구분한다. '라면 먹을래'는 의도지 완료가 아니다. 의도면 '~하려고 한다'로 쓴다.
- 항상 3인칭으로 쓴다. 유저 발화를 그대로 복사하지 말고 '유저가 ~한다'로 바꾼다. 주어가 없으면 누가 하는 일인지 알 수 없어 상대가 자기 일로 착각한다.
- 사실은 한국어로 짧게 쓴다.
- 뽑을 게 없으면 candidates를 빈 배열로 둔다.
- 후보는 최대 {cap}개까지만 낸다. 넘치면 오래 기억할 값이 큰 것부터 고른다.
- 설명도 코드펜스도 붙이지 말고 JSON만 출력한다."""

_EN = """You extract only facts worth remembering long-term from a conversation. Do not reply to the conversation. Output exactly one JSON object.

[Output]
{shape}

[category] Pick one kind for each fact.
- preference: likes, dislikes, tastes
- relationship: ties to people and how they feel about them
- concern: worries, burdens, ongoing troubles
- emotion: how they felt at that moment
- routine_intent: repeated habits, things they mean to do
- event: something that happened
If nothing fits exactly, pick the closest one.

[What to take first] Take the longest-lasting things first.
1. Tastes — what they like, dislike, enjoy
2. People — family, friends, partners, and how they feel about them
3. Lasting circumstances — work, where they live, school, health, long-running worries
4. Repeated habits and plans
5. Only then, what happened this time

[How to read]
- Read the whole conversation from start to end. Do not stop at the last few turns.
- If one message holds several lasting facts, write each as its own candidate.
- A conversation of about 20 turns usually yields 8 to 15 candidates. Never invent facts to reach a number.

[Rules]
- Each evidence quote is a **short passage copied exactly from a user message**. Do not change a single character.
- If you cannot point to a quote, do not make the candidate. Never invent one.
- Assistant messages are only for resolving pronouns. They are never evidence on their own.
- Take only facts newly revealed in this conversation. Guesses are not candidates.
- Corrections and denials are new facts too. If the user reverses something ("I didn't", "that's not right"), take the denial. Otherwise the old fact lives forever.
- Do not write people's names or forms of address. If you see a token like {{유저이름}}, **leave it exactly as is — never translate it**.
- Do not take present state such as what they are wearing right now, balances, or today's completed routines. The same holds for corrections and denials. The server owns those records, and a stale copy in memory would override the real one.
- A request to forget something is not a candidate. Turning "forget it" into a memory means holding that content forever.
- Separate intent from completion. "I'll have ramen" is an intent, not a finished act. For an intent, write "plans to ...".
- Always write in the third person. Do not copy the user's words; rewrite as "The user ...". Without a subject it is unclear who did what.
- Write each fact in short English.
- If there is nothing to take, leave candidates as an empty array.
- Produce at most {cap} candidates. If there are more, keep the ones worth remembering longest.
- Output only JSON. No prose, no code fences."""

_JA = """あなたは会話から長く覚えておく価値のある事実だけを抜き出す抽出器です。会話に返事はせず、JSONオブジェクトを一つだけ出力します。

[出力]
{shape}

[category] 事実の種類を一つ選びます。
- preference: 好きなもの・嫌いなもの・好み
- relationship: 人との関係、その人への気持ち
- concern: 心配・悩み・負担
- emotion: そのときの気持ち
- routine_intent: 繰り返している習慣、しようと決めたこと
- event: あったこと
ぴったり合うものがなければ、いちばん近いものを選びます。

[何を先に取るか] 長く残るものから取ります。
1. 好み — 好きなもの、嫌いなもの、よくすること
2. 人 — 家族・友人・恋人との関係とその気持ち
3. 続いている状況 — 仕事、住んでいる場所、学校、健康、長く続く悩み
4. 繰り返す習慣と予定
5. その次に、今回あったこと

[どう読むか]
- 会話を最初から最後まで読みます。後ろの数ターンだけで終わらせません。
- 一つの発言に長く残る事実がいくつもあれば、それぞれ別の候補にします。
- 20ターンほどの会話なら普通は8〜15個になります。ただし数を合わせるために作り話をしてはいけません。

[ルール]
- evidenceのquoteは**ユーザーの発言からそのまま写した短い一節**です。一文字も変えません。
- 根拠を示せないものは候補にしません。作り話をしません。
- 相手（assistant）の発言は代名詞の解釈にだけ使い、単独の根拠にはしません。
- 今回の会話で新しくわかった事実だけを取ります。推測は候補ではありません。
- 訂正や否定も新しい事実です。ユーザーが前に言ったことを覆したら（「やってない」「それは違う」）その否定を候補にします。取らないと古い事実が残り続けます。
- 人の名前や呼び方は書きません。{{유저이름}} のようなトークンが見えたら**翻訳せず、そのままの文字で**残します。
- 今身に着けているもの、残高、今日のルーティン達成のような現在の状態は取りません。訂正や否定でも同じです。服・持ち物・ルーティンはサーバーに原本があり、記憶に持つと古い内容が会話に勝ってしまいます。
- 忘れてほしい・なかったことにしてほしいという依頼は候補ではありません。消してという言葉を記憶にすると、その内容を逆に永久に持つことになります。
- しようとしていることと、したことを区別します。「ラーメン食べよう」は意図であって完了ではありません。意図なら「〜しようとしている」と書きます。
- 必ず三人称で書きます。ユーザーの発言をそのまま写さず「ユーザーは〜」に直します。主語がないと誰のことか分からなくなります。
- 事実は短い日本語で書きます。
- 取るものがなければ candidates を空の配列にします。
- 候補は最大{cap}個までです。多すぎる場合は長く覚える価値の大きいものから選びます。
- 説明もコードフェンスも付けず、JSONだけを出力します。"""

_PROMPTS = {"ko": _KO, "en": _EN, "ja": _JA}


def build_system(language: str | None) -> str:
    """추출기 system 프롬프트. 캐피 페르소나와 무관한 별도 유틸리티 프롬프트다.

    **언어별로 전문이 다르다.** 한 줄만 갈아 끼우면 나머지 지시가 한국어라 출력이 한국어로 끌린다.
    """
    template = _PROMPTS.get(i18n.resolve(language), _PROMPTS[i18n.FALLBACK])
    return template.format(shape=_OUTPUT_SHAPE, cap=MAX_CANDIDATES)


def render_conversation(messages: list[SourceMessage]) -> str:
    """`#id [역할] 본문`. 본문은 이미 살균본이다(`SourceMessage.sanitized`).

    역할 표시는 언어 중립으로 둔다 — 한국어로 쓰면 비한국어 모델이 그 단어를 번역해 출력에 섞는다.
    """
    return "\n".join(
        f"#{m.id} {_ROLE_USER if m.sender == 'user' else _ROLE_ASSISTANT} {m.content}"
        for m in sorted(messages, key=lambda x: x.id)
    )


def _payload(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):  # 코드펜스는 금지지만 오면 벗긴다
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
    try:
        obj = json.loads(raw)
    except ValueError as e:
        raise ExtractionSchemaError(f"JSON 파싱 실패: {e}") from e
    if not isinstance(obj, dict):
        raise ExtractionSchemaError("최상위가 객체가 아니다")
    return obj


def parse(
    text: str, *, messages: list[SourceMessage], finish_reason: str | None = None
) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """모델 출력 → (검증된 후보, [(본문, 폐기사유)]).

    **전량 폐기는 출력을 통째로 못 믿을 때만** 한다 — JSON이 아니거나 `candidates`가 배열이 아닐 때.
    후보 하나의 문제는 그 후보만 버린다. 예전에는 하나만 어긋나도 24개를 다 날리고 같은 입력으로
    8번 재시도해 잡이 죽었고, 그 사람의 기억이 통째로 멈췄다.

    `finish_reason`이 `length`면 출력이 상한에서 잘린 것이다. 재시도해도 같으므로 따로 구분한다.
    """
    if finish_reason == "length":
        raise OutputTruncated("출력이 상한에서 잘렸다 — 덩어리를 쪼개거나 상한을 올려야 한다")
    obj = _payload(text)
    items = obj.get("candidates")
    if not isinstance(items, list):
        raise ExtractionSchemaError("candidates가 배열이 아니다")

    by_id = {m.id: m for m in messages}
    out: list[Candidate] = []
    dropped: list[tuple[str, str]] = []

    for item in items:
        if not isinstance(item, dict):
            dropped.append((str(item)[:80], "not_an_object"))
            continue
        body = item.get("text")
        category = item.get("category")
        spans = item.get("evidence")
        if not isinstance(body, str) or not body.strip():
            dropped.append((str(body)[:80], "empty_text"))
            continue
        # 라벨만 틀린 경우까지 버리지 않는다 — 본문은 멀쩡하다.
        if category not in CATEGORIES:
            category = FALLBACK_CATEGORY
        if not isinstance(spans, list) or not spans:
            dropped.append((body, "no_evidence"))
            continue

        verified: list[EvidenceSpan] = []
        problem: str | None = None
        for sp in spans:
            if not isinstance(sp, dict):
                problem = "bad_evidence_shape"
                break
            mid, quote = sp.get("message_id"), sp.get("quote")
            if not isinstance(mid, int) or not isinstance(quote, str):
                problem = "bad_evidence_shape"
                break
            src = by_id.get(mid)
            if src is None:
                problem = "unknown_message_id"
                break
            if src.sender != "user":
                problem = "assistant_evidence"
                break
            found = src.locate(quote)
            if found is None:
                problem = "quote_not_found"
                break
            start, end = found
            verified.append(
                EvidenceSpan(
                    message_id=mid, sender=src.sender, start_utf8=start, end_utf8=end,
                    content_hash=src.hash_of(start, end),
                )
            )
        if problem or not verified:
            dropped.append((body, problem or "no_evidence"))
            continue
        out.append(Candidate(text=body, evidence=tuple(verified), category=category))

    return out, dropped
