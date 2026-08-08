"""consolidation classifier — 고정 스키마 판정 파서.

기억 재설계(docs/ARCHITECTURE-capi.md 5.3절).

모델은 `independent | duplicate | supersedes | ambiguous`와 **비교 대상 id만** 낸다.
자유문 판정은 거부한다 — 그걸 받으면 validator가 검사할 대상 자체가 오염된다.

**신규↔신규와 신규↔기존을 한 번에 batch 판정한다.** memory마다 top-k와 classifier를 반복하면
호출 수가 후보 수만큼 늘고 판정이 서로 모순될 수 있다.

거부 정책은 추출기와 같다.
 · JSON이 아니거나 `edges`가 배열이 아니면 → 전량 거부
 · **edge 하나의 문제는 그 edge만 버린다.** 예전에는 하나만 어긋나도 전량 거부 → 8번 재시도 →
   잡이 죽고, 그 턴의 기억이 영원히 미판정으로 남았다(회상에서 아예 안 보인다)
"""
from __future__ import annotations

import json
import logging
import uuid

from app.services import i18n
from app.services.mem0_consolidation import Edge, Verdict

_log = logging.getLogger("moly-worker")

# v2: 근사 중복 duplicate · 부정이 긍정을 supersedes(2026-08-06 감사 지적).
CLASSIFIER_VERSION = "mem0-classifier-v2"

# 비교 대상 개수. 덩어리 하나가 후보를 최대 24건 만들므로 그에 맞춘다.
# 12였을 때는 직전 덩어리의 절반만 비교 대상에 들어왔다.
#
# ⚠️ 이 목록은 **턴 번호가 최근인 것**이지 의미가 비슷한 것이 아니다. 오래전에 한 얘기를 다시
#    하면 여기서는 중복으로 안 잡힌다. 그건 하루 경계 재판정(`worker/reconsolidate_jobs.py`)이
#    맡는다 — 그쪽이 살아 있는 기억을 주기적으로 다시 비교한다.
MAX_EXISTING_CANDIDATES = 24

# 출력 상한. edge 하나가 UUID 두 개 + verdict라 대략 50토큰이다.
#
# ⚠️ 이 호출은 추론 모델(`model_utility`)로 돌기 때문에 **추론을 반드시 꺼야 한다**
#    (`llm.generate(reasoning_effort="none")`). 안 끄면 추론 토큰이 이 상한에서 함께 빠져
#    답이 통째로 잘린다. 운영에서 판정 잡이 그렇게 34번 실패했다.
MAX_OUTPUT_TOKENS = 3_000


class ClassifierSchemaError(ValueError):
    """판정 출력을 통째로 못 믿는다 — 그래프 전체를 쓰지 않는다."""


_KO = """너는 두 기억이 어떤 관계인지 판정한다. 설명하지 말고 JSON 객체 하나만 출력한다.

[출력]
{"edges":[{"subject":"<id>","target":"<id>","verdict":"..."}]}

[verdict]
- independent: 서로 다른 사실. 둘 다 유지
- duplicate: 같은 내용을 다시 말한 것
- supersedes: subject가 target을 정정한 더 새로운 값
- ambiguous: 상충할 수 있으나 어느 쪽이 현재인지 확정 불가

[규칙]
- id는 입력에 있는 것만 쓴다. 새로 만들지 않는다.
- 관계가 없으면 edge를 만들지 않는다. 억지로 잇지 않는다.
- supersedes의 subject는 **반드시 [신규] 쪽**이다. 옛 기억이 새 기억을 대체할 수는 없다.
- 확신이 없으면 ambiguous로 둔다. 단정이 더 위험하다.
- 같은 뜻을 조금 다르게 말한 것은 duplicate다. '~하려고 한다'와 '~하고 싶다'처럼 표현만 다르면 둘 다 남기지 않는다.
- 부정은 긍정을 정정한다. '산책을 안 했다'는 '산책을 갔다'를 supersedes다. 같은 대상에 대한 반대 진술이면 나중 것이 앞선 것을 대체한다.
- 설명도 코드펜스도 붙이지 말고 JSON만 출력한다."""

_EN = """You judge how two memories relate. Do not explain. Output exactly one JSON object.

[Output]
{"edges":[{"subject":"<id>","target":"<id>","verdict":"..."}]}

[verdict]
- independent: different facts. Keep both
- duplicate: the same thing said again
- supersedes: subject is a newer value that corrects target
- ambiguous: they may conflict, but which one holds now cannot be settled

[Rules]
- Use only ids present in the input. Never invent one.
- If there is no relation, emit no edge. Do not force links.
- The subject of a supersedes must be **on the [new] side**. An older memory cannot replace a newer one.
- When unsure, use ambiguous. Being decisive is the riskier error.
- The same meaning worded differently is a duplicate. "plans to go" and "wants to go" differ only in wording; do not keep both.
- A denial corrects an affirmation. "did not go for a walk" supersedes "went for a walk". For opposite statements about the same thing, the later one replaces the earlier.
- Output only JSON. No prose, no code fences."""

_JA = """あなたは二つの記憶がどんな関係かを判定します。説明はせず、JSONオブジェクトを一つだけ出力します。

[出力]
{"edges":[{"subject":"<id>","target":"<id>","verdict":"..."}]}

[verdict]
- independent: 別々の事実。どちらも残す
- duplicate: 同じ内容をもう一度言ったもの
- supersedes: subject が target を訂正した新しい値
- ambiguous: 食い違う可能性はあるが、どちらが今なのか決められない

[ルール]
- idは入力にあるものだけを使います。作りません。
- 関係がなければ edge を作りません。無理につなぎません。
- supersedes の subject は**必ず[新規]の側**です。古い記憶が新しい記憶を置き換えることはありません。
- 確信がなければ ambiguous にします。断定するほうが危険です。
- 同じ意味を少し違う言い方にしただけなら duplicate です。「〜しようとしている」と「〜したい」のように言い方だけ違うなら両方は残しません。
- 否定は肯定を訂正します。「散歩に行かなかった」は「散歩に行った」を supersedes します。同じ対象について反対のことを言っていれば、あとのものが前のものを置き換えます。
- 説明もコードフェンスも付けず、JSONだけを出力します。"""

_PROMPTS = {"ko": _KO, "en": _EN, "ja": _JA}

# 입력 라벨도 언어별로 둔다. 한국어 라벨을 비한국어 프롬프트에 섞으면 모델이 번역하려 든다.
_LABELS = {
    "ko": ("[신규]", "[기존]"),
    "en": ("[new]", "[existing]"),
    "ja": ("[新規]", "[既存]"),
}


def build_system(language: str | None = None) -> str:
    """판정기 system 프롬프트.

    **언어별로 전문이 다르다.** 기억 본문은 유저 언어로 저장되는데 비교 지시만 한국어면,
    한국어 어미 예시('~하려고 한다'와 '~하고 싶다')가 일본어·영어 기억에는 아무 도움이 안 된다.
    """
    return _PROMPTS.get(i18n.resolve(language), _PROMPTS[i18n.FALLBACK])


def render_pairs(
    new_items: list[tuple[uuid.UUID, str]],
    existing: list[tuple[uuid.UUID, str]],
    *,
    language: str | None = None,
) -> str:
    """판정 입력. 신규와 기존을 라벨로 구분해 한 번에 준다."""
    new_label, existing_label = _LABELS.get(i18n.resolve(language), _LABELS[i18n.FALLBACK])
    lines = [new_label]
    lines += [f"{i}: {t}" for i, t in new_items]
    if existing:
        lines.append("")
        lines.append(existing_label)
        lines += [f"{i}: {t}" for i, t in existing]
    return "\n".join(lines)


def parse(text: str, *, known_ids: set[uuid.UUID]) -> list[Edge]:
    """판정 출력 → edge 목록.

    **전량 거부는 출력을 통째로 못 믿을 때만** 한다. edge 하나의 문제는 그 edge만 버린다 —
    예전에는 하나만 어긋나도 전량 거부라 8번 재시도 뒤 잡이 죽었고, 그 턴의 기억은 영원히
    미판정으로 남아 회상에서 아예 안 보였다.
    """
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
    try:
        obj = json.loads(raw)
    except ValueError as e:
        raise ClassifierSchemaError(f"JSON 파싱 실패: {e}") from e
    if not isinstance(obj, dict):
        raise ClassifierSchemaError("최상위가 객체가 아니다")
    items = obj.get("edges")
    if not isinstance(items, list):
        raise ClassifierSchemaError("edges가 배열이 아니다")

    out: list[Edge] = []
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for item in items:
        if not isinstance(item, dict):
            skip("not_an_object")
            continue
        try:
            subject = uuid.UUID(str(item["subject"]))
            target = uuid.UUID(str(item["target"]))
        except (KeyError, ValueError, TypeError):
            skip("bad_id")
            continue
        try:
            verdict = Verdict(item.get("verdict"))
        except ValueError:
            skip("bad_verdict")
            continue
        if subject not in known_ids or target not in known_ids:
            skip("unknown_id")
            continue
        out.append(Edge(subject=subject, target=target, verdict=verdict))
    if skipped:
        _log.info("판정 edge 일부 폐기 %s", skipped)
    return out
