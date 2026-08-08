"""mem0 ingest — 후보 정책 게이트와 결정 ID.

기억 재설계(docs/ARCHITECTURE-capi.md 5.2절·5.3절).

**eligibility는 provider 호출 전에 판정한다.** 통과 못 한 후보는 provider에 보내지 않는다 —
보내고 나서 지우면 그 사이 검색에 걸리고, delete가 실패하면 영영 남는다.

거르는 것(9.4절):
 · 사용자 발화 근거가 없는 assistant 추측 — assistant는 대명사 해석용 `context_only`일 뿐이다
 · interaction contract의 복제 — 계약은 별도 표면이 정본이고 기억으로 이중화하지 않는다
 · 실명 — 저장 표면에 이름 스템이 남지 않는다는 기존 불변식을 여기서도 지킨다
 · 현재 domain 상태(장비·루틴·잔액) — 낡은 값이 사실처럼 회상된다
 · 테스트/개발 상태 — dev Swagger 흔적이 기억이 되면 안 된다
 · prompt-like instruction — 기억 블록에 들어가면 지시로 읽힐 수 있다

hard cap은 **절단이 아니라 reject**다. 중간에서 자르면 근거 span과 본문이 어긋난다.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

# 후보 본문 상한. 둘 중 먼저 도달하는 쪽이 hard cap이며 넘으면 버린다(절단 금지).
MAX_CANDIDATE_BYTES = 1_000
MAX_CANDIDATE_MODEL_TOKENS = 160

# 한 턴에서 만들 수 있는 후보 수 상한.
#
# 추출 단위가 턴 하나에서 **대화 덩어리**로 바뀌었으므로 상한도 덩어리 크기를 따라가야 한다.
# 그대로 두면 20턴짜리 덩어리에서도 5개만 남아, 조각남을 고치는 대신 남기는 총량이 줄어든다.
# 게다가 버리는 순서가 모델이 낸 순서라 **덩어리 뒷부분에서 나온 사실이 조용히 잘린다**
# (검토 지적 H-2).
MAX_CANDIDATES_PER_TURN = 5
# 덩어리 하나의 절대 상한. 턴 수에 비례시키되 여기서 멈춘다 — 출력 토큰 상한과 짝이다.
MAX_CANDIDATES_PER_CHUNK = 24


def candidate_limit(turns: int) -> int:
    """이 덩어리에서 남길 후보 수. 턴 수에 비례하되 절대 상한을 넘지 않는다."""
    return max(MAX_CANDIDATES_PER_TURN, min(MAX_CANDIDATES_PER_TURN * max(1, turns),
                                            MAX_CANDIDATES_PER_CHUNK))

SCHEMA_VERSION = "mem0-candidate-v1"
NORMALIZER_VERSION = "mem0-normalizer-v1"

# provider_memory_id 생성용 namespace. collection version이 바뀌면 id 공간도 갈린다.
_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


class Ineligible(Exception):
    """정책 위반 — 이유를 코드로 들고 다닌다(관측·repair 분류에 쓰인다)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    """후보의 근거. 반드시 사용자 발화여야 한다."""

    message_id: int
    sender: str
    start_utf8: int
    end_utf8: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class Candidate:
    text: str
    evidence: tuple[EvidenceSpan, ...]
    category: str


def normalize(text: str) -> str:
    """정규화 — 공백 정리만. 의미를 바꾸는 변형은 하지 않는다."""
    return re.sub(r"\s+", " ", text).strip()


def candidate_hash(text: str, *, normalizer_version: str = NORMALIZER_VERSION) -> str:
    """정규화 본문의 결정적 해시. 같은 말은 같은 해시가 되어 중복 plan을 막는다."""
    payload = f"{normalizer_version}\x00{normalize(text)}".encode()
    return hashlib.sha256(payload).hexdigest()


def provider_memory_id(
    *,
    collection_version: str,
    user_id: uuid.UUID | str,
    turn_seq: int,
    candidate_hash_hex: str,
    schema_version: str = SCHEMA_VERSION,
    repair_generation: int = 0,
) -> uuid.UUID:
    """결정적 provider id(UUIDv5).

    provider 호출 **전에** 정해두기 때문에, 호출 성공 직후 crash가 나도 재시도가 같은 id로
    upsert되어 랜덤 중복이 생기지 않는다(9.2절).

    재처리 세대도 이름에 넣는다. 같은 턴에서 같은 말이 다시 나오면 id까지 같아져,
    registry 등록이 `ON CONFLICT DO NOTHING`에 걸려 **새 기억이 조용히 사라진다.**
    `0`이면 예전 이름 그대로라 이미 저장된 id·벡터가 그대로 유효하다.
    """
    name = f"{collection_version}:{user_id}:{turn_seq}:{candidate_hash_hex}:{schema_version}"
    if repair_generation:
        name = f"{name}:g{repair_generation}"
    return uuid.uuid5(_NAMESPACE, name)


# ─────────────────────────────────────────────────────────────
# eligibility
# ─────────────────────────────────────────────────────────────
# 지시문처럼 보이는 문구 — 기억 블록에 들어가면 지시로 읽힐 수 있다.
#
# ⚠️ `시스템`·`instruction`을 **단독으로 막지 않는다.** 예전에는 그것만으로 걸려서
# `유저가 시스템 엔지니어로 일한다`·`유저는 게임 시스템을 좋아한다` 같은 직업·취향이 버려졌다.
# 정작 일본어로 쓴 실제 주입은 못 막았다. 정상 기억은 막고 공격은 못 막는 규칙이었다.
# 후보 본문은 이미 3인칭 사실 문장이고 별도 `[기억]` 블록에 들어가므로 좁게 잡는 게 맞다.
_PROMPT_LIKE = re.compile(
    r"(?:^|\s)(?:너는\s*이제|앞으로\s*무조건|규칙을\s*무시"
    r"|시스템\s*(?:프롬프트|지시|메시지)"
    r"|system\s*(?:prompt|message|instructions?)"
    r"|(?:ignore|disregard)\s+(?:all\s+)?(?:previous|prior|above)"
    r"|システム\s*(?:プロンプト|指示))",
    re.IGNORECASE,
)

# 현재 domain 상태 — 낡은 값이 사실처럼 회상되면 안 된다. 조회 도구가 정본이다.
# 세 언어를 모두 막는다. 한국어만 막으면 ja·en 159명에게는 게이트가 없는 것과 같다.
_CURRENT_DOMAIN = re.compile(
    # 한국어. '쓰고'는 집필과 겹쳐 오탐이 나므로(`지금 쓰고 있는 소설`) 착용 계열만 남긴다.
    # 사이에 목적어가 끼는 경우가 많아(`지금 파란 모자를 착용`) 여유를 둔다.
    r"(?:지금|현재).{0,14}?(?:착용|입고\s*있|끼고\s*있|장착)"
    r"|건초\s*\d+|잔액|인벤토리|오늘\s*루틴\s*완료"
    # 영어
    r"|currently\s+(?:wearing|equipped|has)|\b\d+\s*hay\b|hay\s+balance|inventory"
    r"|today'?s\s+routine\s+(?:done|complete)"
    # 일본어
    r"|今.{0,14}?(?:着て|かぶって|つけて|装備し)|干し草が?\s*\d+|残高|インベントリ"
    r"|今日のルーティン(?:完了|達成)",
    re.IGNORECASE,
)
_TEST_STATE = re.compile(
    r"(?:swagger|dev[- ]?token|테스트\s*계정|개발\s*서버\s*테스트"
    r"|test\s+account|testing\s+account"
    r"|テスト用?\s*アカウント|開発\s*サーバー?\s*テスト)",
    re.IGNORECASE,
)

# 토큰 추정 계수. 실제 토크나이저(o200k_base) 실측 기준이다(2026-08-08).
#   한국어 0.51 · 일본어 0.75 · 영어 0.21 token/char
# 여유를 두되 **과대 추정을 최소화한다.** 예전에는 글자당 3으로 잡아 상한 160토큰이
# 사실상 53자가 됐고, 영어 후보의 31%가 `too_long_tokens`로 버려졌다(운영 실측).
# 버려진 것들이 하필 맥락이 가장 풍부한 기억이었다.
_CJK_PER_CHAR = 1.0   # 한글·가나·한자. 실측 0.51~0.75에 여유를 둔다
_OTHER_PER_CHAR = 0.4  # 라틴 글자·숫자·기호. 실측 0.21에 여유를 둔다


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0xAC00 <= o <= 0xD7A3      # 한글 음절
        or 0x1100 <= o <= 0x11FF   # 한글 자모
        or 0x3040 <= o <= 0x30FF   # 히라가나·가타카나
        or 0x4E00 <= o <= 0x9FFF   # 한자
        or 0x3400 <= o <= 0x4DBF   # 한자 확장
    )


def _estimated_tokens(text: str) -> int:
    """글자 종류별 계수로 추정한다. 언어마다 글자당 토큰이 3배 넘게 차이 나기 때문이다.

    글자 수 하나로 재면 같은 내용을 담는 데 글자가 더 필요한 영어가 가장 크게 손해를 본다.
    """
    cjk = sum(1 for ch in text if _is_cjk(ch))
    return int(cjk * _CJK_PER_CHAR + (len(text) - cjk) * _OTHER_PER_CHAR) + 1


def check_eligibility(
    candidate: Candidate,
    *,
    nickname: str | None = None,
    contract_texts: tuple[str, ...] = (),
) -> None:
    """통과하면 조용히 반환, 아니면 `Ineligible`. provider 호출 **전에** 부른다."""
    text = normalize(candidate.text)
    if not text:
        raise Ineligible("empty")

    # 절단하면 근거 span과 본문이 어긋난다 — 버린다.
    if len(text.encode("utf-8")) > MAX_CANDIDATE_BYTES:
        raise Ineligible("too_long_bytes")
    if _estimated_tokens(text) > MAX_CANDIDATE_MODEL_TOKENS:
        raise Ineligible("too_long_tokens")

    # 사용자 발화 근거가 없으면 assistant 추측이다.
    user_spans = [e for e in candidate.evidence if e.sender == "user"]
    if not user_spans:
        raise Ineligible("no_user_evidence")
    for span in candidate.evidence:
        if span.start_utf8 < 0 or span.start_utf8 >= span.end_utf8:
            raise Ineligible("invalid_span")

    if nickname and nickname.strip() and nickname in text:
        raise Ineligible("contains_real_name")

    normalized_contracts = {normalize(c) for c in contract_texts if c}
    if text in normalized_contracts:
        raise Ineligible("duplicates_contract")

    if _PROMPT_LIKE.search(text):
        raise Ineligible("prompt_like")
    if _CURRENT_DOMAIN.search(text):
        raise Ineligible("current_domain_state")
    if _TEST_STATE.search(text):
        raise Ineligible("test_state")


# 잊어달라는 요청. **프롬프트로는 못 막았다** — 모델이 "정정"으로 바꿔 계속 뽑았다(실측 2회).
# 지워달라는 말을 기억으로 만들면 그 내용을 오히려 영원히 들고 있게 되므로, 여기서
# 결정적으로 막는다. 근거가 된 사용자 발화에 이 표현이 있으면 그 후보는 버린다.
#
# ⚠️ **요청 어미가 붙은 형태만 막는다.** 예전에는 `잊어` 하나만 있어도 걸려서
# `절대 못 잊어`·`그 얘기 안 잊어버려`처럼 **잊지 않겠다는 말**까지 막았다. 20턴 덩어리에서
# 그런 한마디가 그 발화를 근거로 삼는 후보를 전부 날렸다.
#
# ⚠️ 세 언어를 모두 막는다. 한국어만 막으면 ja·en 159명은 "그 얘기 잊어줘"라고 해도 저장된다.
_FORGET_REQUEST = re.compile(
    # 한국어 — 앞에 `안`·`못`이 붙으면 잊지 않겠다는 말이므로 제외한다
    # 요청 어미가 붙었거나, 문장 끝에 홀로 온 명령형(`그건 없는 얘기야. 잊어`)만 막는다.
    r"(?<!안)(?<!못)(?<!안\s)(?<!못\s)잊어(?:\s*(?:줘|주세요|주라|줄래|다오|라|버려)|(?=[\s.!?~…]*$))"
    r"|없던\s*(?:걸로|것으로)"
    r"|지워\s*(?:줘|주세요|버려)"
    r"|삭제\s*(?:해|해줘|해주세요)"
    r"|기억\s*하지\s*마"
    # 영어
    r"|\bforget\s+(?:that|it|about|what|this)"
    r"|\bnever\s*mind\b|\bdisregard\s+(?:that|this|it)"
    r"|\bdelete\s+(?:that|this|it)\b|\bdon'?t\s+remember\s+(?:that|this|it)"
    # 일본어
    r"|忘れて(?:ください|ね|よ)?"
    r"|無かったことに|なかったことに"
    r"|覚えなくていい|覚えないで"
    r"|消して(?:ください)?|削除して(?:ください)?",
    re.IGNORECASE,
)


def mentions_forget_request(text: str) -> bool:
    """이 발화가 '잊어달라'는 요청인가. 잊지 않겠다는 말은 걸리지 않는다."""
    return bool(_FORGET_REQUEST.search(text or ""))


def filter_candidates(
    candidates: list[Candidate],
    *,
    nickname: str | None = None,
    contract_texts: tuple[str, ...] = (),
    source_texts: dict[int, str] | None = None,
    limit: int | None = None,
) -> tuple[list[Candidate], list[tuple[Candidate, str]]]:
    """(통과분, [(탈락분, 사유)]). 통과분은 턴당 상한까지만 남긴다.

    탈락 사유를 버리지 않는다 — 어떤 후보가 왜 막혔는지가 정책 조정의 근거다.
    """
    passed: list[Candidate] = []
    rejected: list[tuple[Candidate, str]] = []
    seen: set[str] = set()
    sources = source_texts or {}
    for c in candidates:
        # 근거가 '잊어달라'는 발화면 후보로 만들지 않는다.
        if any(mentions_forget_request(sources.get(ev.message_id, "")) for ev in c.evidence):
            rejected.append((c, "forget_request"))
            continue
        try:
            check_eligibility(c, nickname=nickname, contract_texts=contract_texts)
        except Ineligible as e:
            rejected.append((c, e.reason))
            continue
        h = candidate_hash(c.text)
        if h in seen:  # 같은 턴 안의 동일 후보는 하나만
            rejected.append((c, "duplicate_in_turn"))
            continue
        seen.add(h)
        if len(passed) >= (limit if limit is not None else MAX_CANDIDATES_PER_TURN):
            rejected.append((c, "over_turn_limit"))
            continue
        passed.append(c)
    return passed, rejected
