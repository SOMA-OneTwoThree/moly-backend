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
MAX_CANDIDATES_PER_TURN = 5

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
) -> uuid.UUID:
    """결정적 provider id(UUIDv5).

    provider 호출 **전에** 정해두기 때문에, 호출 성공 직후 crash가 나도 재시도가 같은 id로
    upsert되어 랜덤 중복이 생기지 않는다(9.2절).
    """
    name = f"{collection_version}:{user_id}:{turn_seq}:{candidate_hash_hex}:{schema_version}"
    return uuid.uuid5(_NAMESPACE, name)


# ─────────────────────────────────────────────────────────────
# eligibility
# ─────────────────────────────────────────────────────────────
# 지시문처럼 보이는 문구 — 기억 블록에 들어가면 지시로 읽힐 수 있다.
_PROMPT_LIKE = re.compile(
    r"(?:^|\s)(?:너는\s*이제|앞으로\s*무조건|시스템|system\s*prompt|ignore\s+(?:all\s+)?previous"
    r"|규칙을\s*무시|instruction)",
    re.IGNORECASE,
)
# 현재 domain 상태 — 낡은 값이 사실처럼 회상되면 안 된다. 조회 도구가 정본이다.
_CURRENT_DOMAIN = re.compile(
    r"(?:지금|현재)\s*(?:쓰고|착용|입고|끼고|장착)|건초\s*\d+|잔액|인벤토리|오늘\s*루틴\s*완료"
)
_TEST_STATE = re.compile(r"(?:swagger|dev[- ]?token|테스트\s*계정|개발\s*서버\s*테스트)", re.IGNORECASE)


def _estimated_tokens(text: str) -> int:
    """보수 추정 — 한국어 최악 3 token/char. 실 토크나이저가 붙으면 교체한다.

    과대 추정이라 통과시켜야 할 후보를 막을 수는 있어도, 상한을 넘긴 후보를 통과시키지는 않는다.
    """
    return len(text) * 3


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
_FORGET_REQUEST = re.compile(
    r"(잊어|잊어줘|잊어버려|없던\s*걸로|없던\s*것으로|지워\s*줘|지워줘|삭제해|기억하지\s*마)"
)


def mentions_forget_request(text: str) -> bool:
    """이 발화가 '잊어달라'는 요청인가."""
    return bool(_FORGET_REQUEST.search(text or ""))


def filter_candidates(
    candidates: list[Candidate],
    *,
    nickname: str | None = None,
    contract_texts: tuple[str, ...] = (),
    source_texts: dict[int, str] | None = None,
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
        if len(passed) >= MAX_CANDIDATES_PER_TURN:
            rejected.append((c, "over_turn_limit"))
            continue
        passed.append(c)
    return passed, rejected
