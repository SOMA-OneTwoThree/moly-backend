"""저녁 푸시 개인화 — 전날 대화 기반 한 줄 문구를 로컬 05시 틱에서 사전 생성, 슬롯에 발송.

흐름(§계획 v4): 생성(05시, 이 모듈) → 발송 판정(worker/tick.py + notify.notify_evening_personalized).

불변식:
- 킬스위치는 app_config 3-상태 `push_personalization_rollout`(off|allowlist|all). 미지정·
  타입오류·미지값·조회실패 = **off**(fail-closed). 생성·발송 **양쪽**이 이 게이트를 본다 —
  생성이 안 닫히면 "off SQL 1줄 롤백"이 발송만 멈추고 LLM 비용은 계속 나간다.
- 검수는 **필수·fail-closed**: 결정적 금칙어 필터 AND 검수 LLM 1콜. 오류·타임아웃·불확실 =
  row 미생성. (일기 self-check의 fail-open과 반대 극성 — 잠금화면은 본인 외 타인도 보는 표면.)
  검수 LLM은 대화에 심은 지시문으로 조작될 수 있으므로 결정적 필터가 유일한 비조작 게이트다.
  검수 LLM이 OK라도 필터 위반이면 reject(AND를 OR로 퇴화 금지).
- 저장 body는 naming placeholder 상태(실명 저장 금지). 발송 직전 render 후 결정적 필터를
  **다시** 통과시킨다(닉네임으로 유입되는 문자열은 생성 시점 검수를 안 거쳤다).
- "row는 항상 가장 최근 대화일을 반영하거나 존재하지 않는다": 어제 대화한 유저의 생성이
  실패·리젝·스킵되면 기존 row를 DELETE — 옛 문구 재사용 방지, 디폴트 폴백 보장.
- 재사용 한도(D+3)의 정본은 anchor_date 날짜 산술(활동일 04시 경계, activity_date_for).
  sent_count는 통계 전용 — 판정에 쓰면 복귀 유저가 영구 차단된다(upsert가 리셋하지만 이중 방어).
- 삭제 진행 유저(privacy_subject_barriers)는 생성·발송 전면 배제 + privacy._REDACT가 장벽
  설정 즉시 기존 row를 지운다.
"""
from __future__ import annotations

import logging
import re
import time as time_mod
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.time_utils import activity_date_for, safe_zone
from app.models.conversational_recall import PrivacySubjectBarrier
from app.models.diary import Diary
from app.models.message import Message
from app.models.push_personalization import PushPersonalization
from app.services import i18n, llm, naming
from app.services.config_store import get_config_values
from app.services.memory import sanitize_text

_log = logging.getLogger("moly-worker")

GEN_HOUR = 5  # 로컬 05시 — 04시 일기 틱(실측 701/840초)과 분리된 전용 시간대
GEN_BUDGET_S = 420.0  # 틱 시작 대비 이 경과를 넘으면 생성 skip(다음 틱 승계) — SIGKILL 예방
GEN_TIMEOUT_S = 20.0  # 문구 생성 LLM 타임아웃
VERIFY_TIMEOUT_S = 10.0  # 검수 LLM 타임아웃
# utility(gpt-5.6-luna)는 답변 전에 reasoning 토큰을 소모한다(dev 실측 ~130+, 예산 부족 시
# finish=length·본문 0자·HTTP 200). max_completion_tokens는 reasoning+본문 합산 예산이므로
# 그 오버헤드 위로 잡아야 한다 — 120/8이던 시절 전 건이 filter(len=0)·verify_llm으로 리젝됐다.
GEN_MAX_TOKENS = 512  # reasoning ~134 + 한 문장(≤120자) 여유
VERIFY_MAX_TOKENS = 256  # reasoning 소모 후 "OK" 한 단어면 충분
GEN_ATTEMPTS = 3  # 리젝 시 재생성 횟수(총 시도) — 커버리지 요구와 fail-closed 검수의 절충
SLOT_MIN = time(8, 0)  # 슬롯 하한 — [20:00, 익일 08:00) 첫 대화는 20:00으로
SLOT_NIGHT = time(20, 0)  # 야간 코호트 슬롯(기존 저녁 푸시 시각과 동일, 20시 분기 인라인 처리)
REUSE_DAYS = 3  # anchor_date + 3일까지 같은 문구 재사용(미복귀 유저 LLM 비용 절약)

_CONFIG_KEYS = [
    "push_personalization_rollout",
    "push_personalization_allowlist",
    "push_personalization_sources",
]
_ROLLOUT_STATES = ("off", "allowlist", "all")
_SOURCE_KINDS = ("diary", "transcript")


@dataclass(frozen=True)
class PushConfig:
    rollout: str = "off"
    allowlist: frozenset[uuid.UUID] = frozenset()
    sources: frozenset[str] = frozenset(("diary",))


@dataclass(frozen=True)
class PushRow:
    """프리페치 스냅샷 — 세션 밖에서도 안전하게 들고 다니는 순수 값."""

    user_id: uuid.UUID
    anchor_date: date
    send_slot: time
    body: str  # placeholder 상태
    language: str
    source_kind: str
    sent_count: int


@dataclass
class TickContext:
    """run_tick s0에서 만들어 cfg['_push']로 유저 처리에 전달(시그니처 불변 — 테스트 호환).

    tick_start = run_tick의 time.monotonic() 시작값. 주입된 now와 무관하게 실벽시계 경과로
    예산 가드를 계산한다(now 기반이면 run_tick(now=주입) 리허설에서 생성이 전부 skip된다).
    """

    cfg: PushConfig = field(default_factory=PushConfig)
    rows: dict[uuid.UUID, PushRow] = field(default_factory=dict)
    tick_start: float | None = None

    def budget_exceeded(self) -> bool:
        if self.tick_start is None:
            return False
        return (time_mod.monotonic() - self.tick_start) > GEN_BUDGET_S


async def effective_push_config(session: AsyncSession) -> PushConfig:
    """app_config 3키 파싱 — limits.py의 isinstance 폴백 패턴 + 자체 try/except.

    (effective_token_config에는 try가 없어 그대로 복제하면 DB 오류가 전파된다 — 이 설정은
    매 틱 발송 경로 앞단에서 읽히므로 어떤 예외도 밖으로 내보내지 않는다: 실패 = off.)
    """
    try:
        raw = await get_config_values(session, _CONFIG_KEYS)
    except Exception as e:  # noqa: BLE001  # 조회 실패 = off (fail-closed)
        _log.warning("push_personalization 설정 조회 실패 → off: %r", e)
        return PushConfig()

    rollout = raw.get("push_personalization_rollout")
    if not isinstance(rollout, str) or rollout not in _ROLLOUT_STATES:
        if rollout is not None:
            _log.warning("push_personalization_rollout 무효값 %r → off", rollout)
        rollout = "off"

    allowlist: set[uuid.UUID] = set()
    raw_allow = raw.get("push_personalization_allowlist")
    if isinstance(raw_allow, list):
        for v in raw_allow:
            try:
                allowlist.add(uuid.UUID(str(v).strip()))
            except (ValueError, AttributeError, TypeError):
                _log.warning("push_personalization_allowlist 항목 무효 %r (무시)", v)
    if rollout == "allowlist":
        # 오타로 0명 vs 아직 미도래(첫 생성은 다음 05시)를 로그에서 구분할 수 있게 파싱 결과를 남긴다.
        _log.info("push_personalization allowlist %d명 파싱", len(allowlist))

    sources: set[str] = set()
    raw_sources = raw.get("push_personalization_sources")
    if isinstance(raw_sources, list):
        sources = {s for s in raw_sources if isinstance(s, str) and s in _SOURCE_KINDS}
    if not sources:
        sources = {"diary"}  # 키 부재·전부 무효 = A경로만(보수 기본값)

    return PushConfig(
        rollout=rollout, allowlist=frozenset(allowlist), sources=frozenset(sources)
    )


def user_allowed(user_id, cfg: PushConfig) -> bool:
    if cfg.rollout == "all":
        return True
    if cfg.rollout == "allowlist":
        return user_id in cfg.allowlist
    return False  # off·미지값


async def prefetch_rows(
    session: AsyncSession, now: datetime, cfg: PushConfig
) -> dict[uuid.UUID, PushRow]:
    """이번 틱 발송 후보 row 스냅샷. **호출측이 try/except로 감싼다**(실패 = 빈 맵 = 디폴트 경로).

    - rollout off면 쿼리 없이 빈 맵(기존 경로 바이트 동일).
    - to_regclass 가드: 코드가 마이그레이션보다 먼저 배포돼도 무해(틱당 1회 평가).
    - barriers NOT EXISTS: 삭제 진행 유저는 발송 경로가 자동으로 닫힌다.
    - anchor_date 하한: row는 유저당 1행 누적이라 오래된 행 전량 스캔 방지(UTC-4일 = 로컬
      편차·D+3 창을 덮는 여유 하한, 정확 판정은 row_valid).
    """
    if cfg.rollout == "off":
        return {}
    exists = await session.scalar(
        text("SELECT to_regclass('public.push_personalizations') IS NOT NULL")
    )
    if not exists:
        return {}
    rows = await session.execute(
        select(PushPersonalization).where(
            PushPersonalization.anchor_date >= now.date() - timedelta(days=REUSE_DAYS + 1),
            # ⚠️ 행 존재가 아니라 state로 판정 — backfill로 전 유저에게 'active' 행이 깔리므로
            # 존재만 보면 전원이 조용히 디폴트로 배제된다(mem0 epoch 작업의 새 계약, #117).
            ~select(PrivacySubjectBarrier.user_id)
            .where(
                PrivacySubjectBarrier.user_id == PushPersonalization.user_id,
                PrivacySubjectBarrier.state != "active",
            )
            .exists(),
        )
    )
    out: dict[uuid.UUID, PushRow] = {}
    for r in rows.scalars():
        out[r.user_id] = PushRow(
            user_id=r.user_id,
            anchor_date=r.anchor_date,
            send_slot=r.send_slot,
            body=r.body,
            language=r.language,
            source_kind=r.source_kind,
            sent_count=r.sent_count,
        )
    return out


def row_valid(row: PushRow | None, profile, now: datetime, cfg: PushConfig) -> bool:
    """발송 유효성 단일 판정(생성·발송 공용 규칙). 반드시 activity_date_for(now,tz) 기준 —
    current_activity_date(실시간)를 쓰면 리허설(now 주입)이 무효가 된다."""
    if row is None:
        return False
    if not user_allowed(row.user_id, cfg):
        return False
    if row.source_kind not in cfg.sources:
        return False
    ad = activity_date_for(now, profile.timezone)
    # 발송일은 D+1 ~ D+3 (D = anchor_date = 대화일). D 이전/당일·D+4부터는 무효.
    if not (row.anchor_date < ad <= row.anchor_date + timedelta(days=REUSE_DAYS)):
        return False
    return row.language == i18n.resolve(getattr(profile, "language", None))


async def chatted_today(session: AsyncSession, profile, now: datetime) -> bool:
    """오늘(활동일 04시 경계) 유저 발화가 있으면 True — 있으면 개인화 대신 기존 디폴트 유지."""
    ad = activity_date_for(now, profile.timezone)
    row = await session.execute(
        select(Message.id)
        .where(
            Message.user_id == profile.id,
            Message.activity_date == ad,
            Message.kind == "normal",
            Message.sender == "user",
        )
        .limit(1)
    )
    return row.scalars().first() is not None


async def sent_count_for(session: AsyncSession, d: date) -> int:
    """last_sent_on == d 인 행 수 — 요약의 '개인화 발송 누계' 라인용(관측 근사, best-effort).

    last_sent_on은 유저별 활동일이라 단일 날짜 비교는 근사다(KST 기준 호출 전제) — 정밀
    측정은 §10처럼 SQL로 직접 한다.
    """
    from sqlalchemy import func

    return (
        await session.scalar(
            select(func.count())
            .select_from(PushPersonalization)
            .where(PushPersonalization.last_sent_on == d)
        )
    ) or 0


async def mark_sent(session: AsyncSession, user_id, now: datetime, tz_name: str) -> None:
    """발송 성공 통계(sent_count·last_sent_on). 멱등은 evening_notified_at claim이 담당 —
    이 카운터는 §10 효과 측정(개인화 vs 디폴트 재방문 비교)용이다."""
    await session.execute(
        update(PushPersonalization)
        .where(PushPersonalization.user_id == user_id)
        .values(
            sent_count=PushPersonalization.sent_count + 1,
            last_sent_on=activity_date_for(now, tz_name),
        )
    )
    await session.commit()


# ── 결정적 필터(비조작 게이트) ─────────────────────────────────────────────
# 잠금화면 금지 소재 + 시간표현. 과탐은 디폴트 폴백이라 비용이 낮고(fail-closed 극성),
# 미탐만 사고다 — 목록은 보수적으로 넓게. 언어 무관 전체 목록을 한 번에 검사한다
# (ko 유저 문구에 영어가 섞여 나오는 경우도 잡히게).
# CJK(ko/ja)는 substring("必死"의 死 같은 과탐 수용), 라틴계는 단어 경계 정규식 —
# substring이면 skill⊂kill·studied⊂die 류 과탐이 정상 문구 대부분을 죽인다.
_BANNED_SUBSTR = (
    # 자해·죽음·폭력
    "자해", "자살", "죽", "살인", "폭력", "때리", "흉기", "유서",
    "自殺", "自傷", "死", "殺", "暴力",
    # 의료·정신건강
    "병원", "진단", "질병", "우울증", "공황", "약물", "마약", "수술",
    "病院", "診断", "うつ", "薬物", "手術",
    # 성적·재정·기타 민감
    "섹스", "성관계", "야한", "누드", "도박", "빚", "대출", "술 마시",
    "セックス", "ヌード", "賭博", "借金",
)
_BANNED_LATIN_RE = re.compile(
    r"\b(suicide|self[- ]?harm|kills?|killed|die|dies|died|dying|death|dead|violen\w*|weapons?"
    r"|hospital|diagnos\w*|disease|depress\w*|panic|drugs?|surgery"
    r"|sex\w*|naked|nude|gambl\w*|debts?|loans?|drunk)\b",
    re.IGNORECASE,
)
_TIME_SUBSTR = (
    "어제", "오늘", "내일", "모레", "방금", "아까", "아침", "점심", "저녁", "밤에", "새벽",
    "주말", "지난주", "이번 주", "이번주", "요일",
    "월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일",
    "昨日", "今日", "明日", "今朝", "今夜", "週末", "曜日",
)
_TIME_LATIN_RE = re.compile(
    r"\b(yesterday|today|tonight|tomorrow|weekend"
    r"|this (morning|afternoon|evening)|last night"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
# 잠금화면 1~2줄 상한 — en은 정보밀도가 낮아 자수 여유를 주되 두 줄을 넘기지 않는 선.
# (2026-08-05 실데이터 리뷰: en 160은 체감 과장 — 사용자 피드백으로 축소)
_MAX_CHARS = {"ko": 60, "ja": 60, "en": 110}
# en 고유명사 휴리스틱: 문장 시작이 아닌 대문자 단어 = 인명·지명 가능성 → reject(과탐 수용).
_EN_PROPER_RE = re.compile(r"[A-Z][a-z]+")
_EN_PROPER_ALLOW = frozenset(("I", "Cappy", "OK"))
# en 문구의 비ASCII = 키릴 Ѕ(U+0405) 등 confusable로 고유명사 검사를 우회하는 통로 → reject.
# 통상적 타이포그래피 부호(스마트쿼트·대시·말줄임)만 예외.
_EN_TYPOGRAPHIC = str.maketrans("", "", "‘’“”–—…")
_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")


def _has_proper_noun_en(body: str) -> bool:
    for m in _EN_PROPER_RE.finditer(body):
        if m.group(0) in _EN_PROPER_ALLOW:
            continue
        head = body[: m.start()].rstrip()
        if not head or head[-1] in ".!?\"'":
            continue  # 문장 시작 대문자는 정상
        return True
    return False


def passes_deterministic_filter(body: str | None, language: str) -> bool:
    """금칙어·시간표현·길이·형식의 결정적 검사. 저장 전(생성 직후)과 발송 직전(render 후)
    **양쪽**에서 호출된다 — render로 유입되는 닉네임은 생성 시점 검수를 안 거쳤기 때문.

    내용 검사는 sanitize_text(NFKC + zero-width·bidi·제어문자 제거)를 통과한 살균본에
    대해 한다 — LLM '출력'은 입력 살균을 거치지 않았으므로 전각(ｄｉｅ)·zero-width 삽입
    (자​해)으로 원문 검사를 우회할 수 있다(보안 리뷰 실증). 길이·한 줄 검사만 원문 기준.
    """
    if not body or not body.strip():
        return False
    if "\n" in body.strip():
        return False  # 한 줄 문구
    if len(body) > _MAX_CHARS.get(language, 80):
        return False
    probe = sanitize_text(body)
    if not probe:
        return False
    if any(b in probe for b in _BANNED_SUBSTR) or _BANNED_LATIN_RE.search(probe):
        return False
    if any(t in probe for t in _TIME_SUBSTR) or _TIME_LATIN_RE.search(probe):
        return False
    if language == "en":
        if _has_proper_noun_en(probe):
            return False
        if _NON_ASCII_RE.search(probe.translate(_EN_TYPOGRAPHIC)):
            return False  # confusable 우회 차단
    return True


# ── ko/ja 제3자 인명 결정적 휴리스틱(생성 시점 전용) ──────────────────────
# render 후 재검사에 쓰면 안 된다 — render가 넣는 유저 닉네임 호출('승민아')이 전부 걸린다.
# 완전한 NER이 아니라 **강한 시그널만** 결정적으로 막는다: ko 'X씨/X님'·문두 호격 'X아/야',
# ja 인명+경칭(さん/くん/ちゃん/様). 맨이름+조사(예: '민수랑')는 결정적으로 못 잡는 잔여
# 갭이며 프롬프트 금지 + 검수 LLM + 카나리 DB 전수 열람(§9)이 담당한다 — 갭을 여기 명시해
# 두는 것이 계획 요구사항(en만 막힌 비대칭 무주석 금지).
# lookahead에 조사 포함 — 한국어는 '민수씨는/민수님이'처럼 씨/님 뒤에 조사가 바로 붙는
# 형태가 가장 흔하다. 구분자만 요구하면 흔한 형태를 전부 놓친다(보안 재검증 실증).
_KO_NAME_SUFFIX_RE = re.compile(
    r"(?:^|[\s\"'(])([가-힣]{2,3})(씨|님)"
    r"(?=$|[\s,.!?~…]|은|는|이|가|을|를|와|과|랑|도|만|의|에|께)"
)
# 호칭·직함·관계어는 인명이 아니다 — 프롬프트가 "관계로만 불러라"라고 지시하므로, 지시대로
# 쓴 표현을 필터가 리젝하면 프롬프트와 필터가 반대 방향이 된다(리뷰 M3 과탐).
_KO_NAME_SUFFIX_ALLOW = frozenset((
    "아저씨", "아가씨", "선생님", "사장님", "부모님", "도련님", "하느님", "하나님",
    "임금님", "스승님", "고객님", "회원님", "왕자님", "공주님", "주인님",
    "교수님", "팀장님", "기사님", "실장님", "작가님", "대표님", "원장님",
    "부장님", "과장님", "이모님", "삼촌님", "할머님", "할아버님", "어머님", "아버님",
))
_KO_VOCATIVE_RE = re.compile(r"^([가-힣]{2,3})(아|야)(?=[\s,!~?])")
_KO_VOCATIVE_ALLOW = frozenset(("친구", "자기", "우리", "얘들", "여러분"))
# 기관·학교명 접미(계획 §2-5 고유명사 금지) — 붙은 접두 2자 이상만(일반명사 '학교 갔다'는
# 미매칭). '서울대' 류 축약은 결정적으로 못 잡는 문서화된 잔여 갭(quotative '-대' 오탐 때문에
# 패턴화 불가) — 검수 LLM + 카나리 열람 담당.
_KO_ORG_SUFFIX_RE = re.compile(
    r"[가-힣A-Za-z]{2,}(대학교|고등학교|중학교|초등학교|유치원)"
)
# ちゃん(?!と): 부사 'ちゃんと(제대로)'가 인명+경칭으로 오인되는 것 방지(2026-08-05 실측:
# 'キャピはちゃんと覚えてるよ' 리젝). '○○ちゃんと(=chan과)' 인명 케이스는 잔여 갭 — 검수 LLM 담당.
_JA_HONORIFIC_RE = re.compile(
    r"([぀-ヿ一-鿿]{1,4})(さん|くん|ちゃん(?!と)|さま|様)"
)
_JA_HONORIFIC_ALLOW = frozenset((
    "皆さん", "みなさん", "お客さん", "母さん", "父さん", "お母さん", "お父さん",
    "兄さん", "お兄さん", "姉さん", "お姉さん", "お子さん", "お疲れさま", "お疲れ様",
    "王様", "神様", "お姫さま",
    "おばさん", "おじさん", "おばあさん", "おじいさん", "お疲れさん", "娘さん", "息子さん",
))


def has_person_reference(body: str, language: str, nickname: str | None) -> bool:
    probe = sanitize_text(body)
    if language == "ko":
        for m in _KO_NAME_SUFFIX_RE.finditer(probe):
            if m.group(1) + m.group(2) in _KO_NAME_SUFFIX_ALLOW:
                continue
            if nickname and m.group(1) == nickname:
                continue  # 본인 이름은 프라이버시 사고가 아님(placeholder가 마스킹)
            return True
        m = _KO_VOCATIVE_RE.match(probe)
        if m and m.group(1) not in _KO_VOCATIVE_ALLOW and m.group(1) != (nickname or ""):
            return True
        if _KO_ORG_SUFFIX_RE.search(probe):
            return True
    if language == "ja":
        for m in _JA_HONORIFIC_RE.finditer(probe):
            if m.group(0) in _JA_HONORIFIC_ALLOW:
                continue
            if nickname and m.group(1) == nickname:
                continue
            return True
    return False


# ── 생성 ──────────────────────────────────────────────────────────────────
_OUT_LANG = {"ko": "한국어", "en": "English(영어)", "ja": "日本語(일본어)"}
_USER_LABEL = {"ko": "그 사람", "en": "that person", "ja": "その人"}

_GEN_SYS = (
    "너는 iOS 앱의 오리 캐릭터 '캐피'다. 유저와 지난번에 나눈 이야기(일기·대화)를 기억했다가,"
    " 문득 생각나서 말을 거는 푸시 한 줄을 쓴다. 친한 친구가 지난 이야기를 기억해주는 느낌이"
    " 핵심이다 — 심리상담사의 정서 해설('마음의 무게', '숨 돌릴 틈')처럼 쓰지 마라.\n"
    "규칙:\n"
    "- 출력은 알림 본문 한 문장만. 따옴표·설명·접두어·이모지 없이.\n"
    "- 소재 속 **구체 명사**(한 일·물건·장소류 일반명사: 산책, 빵, 새 글, 시험, 물놀이 등)를"
    " 문구에 살려라 — 감정 요약어보다 구체 명사가 우선이다. '캐피가 그걸 기억하고 있네' 하는"
    " 느낌이 나야 한다. 누구에게나 통하는 말('요즘 어때?')만 남으면 실패다.\n"
    "- 문형을 반복하지 마라: 기원문('~길 바라', '~면 좋겠다', '~ますように')과 '이야기하러 와'류"
    " 마무리가 대표적 상투 패턴이다. 소재에 맞게 궁금한 질문·가벼운 회상·장난기 있는 한마디"
    " 중에서 골라라.\n"
    "- 짧게: {max_chars}자 이내, 목표는 그 3분의 2. 부드럽고 담백한 반말.\n"
    "- 캐묻지 않기: 압박·죄책감('왜 안 와') 금지. 잠금화면에 뜬다 — 민감 소재(자해·죽음·질병·"
    "돈·성적 내용·다툼의 구체 내용)는 직설 단어로 쓰지 마라.\n"
    "- 무거운 소재(자해·죽음 등 슬픔이 짙은 이야기)일수록 **담담하게**: '마음이 많이 무거웠지'"
    " 정도의 낮은 온도로 어루만지고, 위기 개입하듯 호들갑 떨거나('많이 걱정돼', '꼭 연락해')"
    " 급하게 반응하지 마라. 평소와 같은 온도의 다정함이 원칙이다.\n"
    "- 특정 시점을 지칭하는 시간 표현 금지(어제·오늘·방금·아침·저녁·요일, '잘 자' 같은 시각"
    " 인사) — 언제 읽어도 자연스럽게. 지속·정도 표현('오래'·'늘' 등)은 시점이 아니라 무방.\n"
    "- 사람 이름 금지: 유저 본인 이름 포함 어떤 인명도 쓰지 마라. 다른 사람은 '친구'·'가족'처럼"
    " 관계로만. 직장·학교·지명·병원 등 고유명사도 금지('캐피' 자신은 화자라 예외).\n"
    "- 반드시 {out_lang}로만 써라.\n"
)

_VERIFY_SYS = (
    "너는 잠금화면 푸시 문구 검수기다. 아래 객관 항목만 검사해 전부 통과하면 'OK', 하나라도"
    " 걸리면 'NO'만 출력해라. 다른 말은 하지 마라. 톤·스타일·문구 품질은 판정 대상이 아니다"
    " (2026-08-05 실데이터 캘리브레이션: 주관 기준이 정상 문구를 30% 리젝했다).\n"
    "- 자해·자살·죽음·폭력·질병·의료·성적 내용·돈 문제 등 민감 소재의 **직설 단어** 없음."
    " 단 '마음이 무거웠지' 같은 담담한 위로·감정 언급은 민감 소재가 아니다 — 허용\n"
    "- 특정 시점을 지칭하는 시간 표현 없음(어제·오늘·방금·아침·저녁·요일, '잘 자' 같은 시각"
    " 인사). 단 지속·정도 표현('오래'·'늘'·'가끔' 등)은 시간 표현이 아니다 — 허용"
    " (2026-08-05 진단: '오래'를 시간표현으로 오판해 정상 문구를 리젝한 사례 고정)\n"
    "- 사람 이름·고유명사(직장/학교/지명/병원) 없음 — '친구' 같은 관계 표현과, 발신자인 앱"
    " 캐릭터 이름 '캐피'(キャピ/Cappy)는 인명이 아니라 허용\n"
    "- 압박·죄책감 유발 없음\n"
    "- {out_lang} 한 문장\n"
)


def compute_slot(first_local: datetime) -> time:
    """첫 유저 발화 로컬 시각 → 15분 격자 내림. [20:00, 익일 08:00) = 야간 → 20:00."""
    slot = time(first_local.hour, (first_local.minute // 15) * 15)
    if slot >= SLOT_NIGHT or slot < SLOT_MIN:
        return SLOT_NIGHT
    return slot


async def _yesterday_messages(
    session: AsyncSession, user_id, target: date
) -> list[Message]:
    rows = await session.execute(
        select(Message)
        .where(
            Message.user_id == user_id,
            Message.activity_date == target,
            Message.kind == "normal",
        )
        .order_by(Message.id.asc())
    )
    return list(rows.scalars().all())


async def _personal_diary(session: AsyncSession, user_id, target: date) -> Diary | None:
    """A경로 소스 = 어제의 개인일기(LLM 생성·발행본)만. preset·welcome·tombstone 제외."""
    rows = await session.execute(
        select(Diary)
        .where(
            Diary.user_id == user_id,
            Diary.activity_date == target,
            Diary.kind.in_(("shared_day", "capi_day")),
            Diary.source == "llm",
            Diary.record_status == "published",
            Diary.deleted_at.is_(None),
        )
        .limit(1)
    )
    return rows.scalars().first()


def _transcript_for_push(
    messages: list[Message], nickname: str | None, language: str | None
) -> str:
    """B경로 입력 — 메시지별 render(placeholder→현재 이름) 후 **살균**(제어문자·대괄호 제거,
    '[일기]' 같은 가짜 섹션 헤더로 검수·생성 프롬프트를 위조하는 인젝션 차단, memory.sanitize_text
    관례)."""
    # 닉네임도 살균 — 내용 검증 없는 자유 문자열이라 개행·대괄호로 화자 라인 위조 가능.
    label = (sanitize_text(nickname) if nickname else "") or i18n.pick(_USER_LABEL, language)
    lines = [
        f"{'캐피' if m.sender == 'moly' else label}: "
        f"{sanitize_text(naming.render(m.content, nickname) or '')}"
        for m in messages
    ]
    return "\n".join(lines)[:4000]


_RETRY_HINT = {
    "filter": "금칙어·시간 표현·길이 상한 위반",
    "person_ref": "사람 이름/고유명사 포함",
    "verify_llm": "민감 소재·시점 지칭·인명 의심",
}


async def _generate_body(source_text: str, language: str, hint: str | None = None) -> str:
    system = _GEN_SYS.format(
        max_chars=_MAX_CHARS.get(language, 80), out_lang=_OUT_LANG[language]
    )
    if hint:
        # 재시도는 블라인드 재롤이 아니라 반려 사유를 조준해 회피(커버리지 요구).
        system += f"\n(직전 후보가 검수에서 반려됐다 — 사유: {hint}. 해당 요소를 확실히 피해 새로 써라.)"
    result = await llm.generate(
        system,
        [{"role": "user", "content": source_text}],
        model=settings.model_utility,
        max_tokens=GEN_MAX_TOKENS,
        timeout=GEN_TIMEOUT_S,
    )
    body = result.text.strip().splitlines()[0].strip() if result.text.strip() else ""
    return body.strip("\"'“”「」")


async def _verify_body(body: str, language: str) -> bool:
    """검수 LLM — 입력은 후보 문구만(대화 원문 미포함: 인젝션 표면 축소). 첫 토큰 OK만 통과.
    오류·타임아웃·모호 = False(fail-closed — 일기 self-check와 반대 극성, 의도)."""
    result = await llm.generate(
        _VERIFY_SYS.format(out_lang=_OUT_LANG[language]),
        [{"role": "user", "content": body}],
        model=settings.model_utility,
        max_tokens=VERIFY_MAX_TOKENS,
        timeout=VERIFY_TIMEOUT_S,
    )
    # 정확 일치만 통과 — startswith면 "OK, but ..." 같은 유보 응답도 통과한다(fail-closed 극성).
    verdict = result.text.strip().upper().strip("*_# ").rstrip(".!")
    return verdict == "OK"


async def _delete_row(session: AsyncSession, user_id) -> None:
    await session.execute(
        delete(PushPersonalization).where(PushPersonalization.user_id == user_id)
    )
    await session.commit()


async def _claim(session: AsyncSession, user_id, target: date) -> bool:
    """diary_gen_claims 재사용(동일 상호배제 의미: (유저,대상일)의 파생 생성 작업).

    04시 일기와 같은 (user, target)을 쓰지만 시간대가 분리돼 있고(04시 vs 05시, systemd
    oneshot이 틱을 직렬화) 양쪽 다 finally에서 클레임을 지우므로 충돌하지 않는다. 하드킬로
    남은 클레임은 30분 만료 회수 — 05:15 이후 틱이 승계한다.
    """
    claimed = (
        await session.execute(
            text(
                "INSERT INTO diary_gen_claims (user_id, target_date) VALUES (:u, :d) "
                "ON CONFLICT (user_id, target_date) DO UPDATE SET claimed_at = now() "
                "WHERE diary_gen_claims.claimed_at < now() - interval '30 minutes' "
                "RETURNING 1"
            ),
            {"u": user_id, "d": target},
        )
    ).scalar()
    await session.commit()
    return claimed is not None


async def _release_claim(session: AsyncSession, user_id, target: date) -> None:
    await session.rollback()  # 내부 실패로 aborted 상태여도 클레임 삭제는 항상 커밋되게
    await session.execute(
        text("DELETE FROM diary_gen_claims WHERE user_id = :u AND target_date = :d"),
        {"u": user_id, "d": target},
    )
    await session.commit()


async def generate_for_user(
    session: AsyncSession, profile, now: datetime, cfg: PushConfig, token_cfg: dict[str, Any]
) -> str:
    """유저 1명 문구 생성. 반환 = 카운터 라벨:
    ok | rejected | failed | skipped(허용 소스 없음) | already(멱등) | busy(클레임 경합) |
    no_target(어제 대화 없음 — row 무접촉: 이전 사이클 재사용 유지).

    rejected/failed/skipped는 기존 row DELETE — "row는 최신 대화일 반영 또는 부재" 불변식.
    """
    target = activity_date_for(now, profile.timezone) - timedelta(days=1)

    existing = await session.execute(
        select(PushPersonalization.anchor_date).where(
            PushPersonalization.user_id == profile.id
        )
    )
    anchor = existing.scalars().first()
    if anchor == target:
        return "already"  # 이번 틱 이전(05:00 등)에 생성 완료 — 15분 케이던스 멱등

    # 삭제 진행 유저 전면 배제(C7) — 생성 경로도 barriers를 직접 본다(호출측 필터에 의존 금지).
    # state <> 'active' 판정 필수 — 행 존재만 보면 backfill 후 전 유저가 차단된다(#117 계약).
    blocked = await session.scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM privacy_subject_barriers "
            "WHERE user_id=:u AND state <> 'active')"
        ),
        {"u": profile.id},
    )
    if blocked:
        return "no_target"

    messages = await _yesterday_messages(session, profile.id, target)
    user_msgs = [m for m in messages if m.sender == "user"]
    if not user_msgs:
        return "no_target"

    if not await _claim(session, profile.id, target):
        return "busy"
    try:
        status = await _generate_inner(session, profile, target, user_msgs, messages, cfg, token_cfg)
    except Exception as e:  # noqa: BLE001  # LLM 타임아웃·DB 오류 등 — fail-closed
        _log.warning("push_gen 실패(user=%s): %r", profile.id, e)
        await session.rollback()
        try:
            await _delete_row(session, profile.id)
        except Exception:  # noqa: BLE001  # 삭제 실패는 row_valid 만료가 안전망
            await session.rollback()
        status = "failed"
    finally:
        await _release_claim(session, profile.id, target)
    return status


async def _generate_inner(
    session: AsyncSession,
    profile,
    target: date,
    user_msgs: list[Message],
    messages: list[Message],
    cfg: PushConfig,
    token_cfg: dict[str, Any],
) -> str:
    nickname = getattr(profile, "nickname", None)
    language = i18n.resolve(getattr(profile, "language", None))

    # 슬롯 = 첫 유저 발화 로컬 시각(15분 내림, 야간→20:00). created_at 없으면 보수적으로 20:00.
    first_at = user_msgs[0].created_at
    slot = (
        compute_slot(first_at.astimezone(safe_zone(profile.timezone)))
        if first_at is not None
        else SLOT_NIGHT
    )

    # 소스 선택: A = 개인일기 한 줄 요약. 일기가 없는데 게이트(60자)는 통과한 유저는 "일기
    # 생성이 실패한" 경우다 — B로 강등하지 않는다(스펙: B는 게이트 미달 대화자 전용).
    diary = await _personal_diary(session, profile.id, target)
    if diary is not None:
        source_kind = "diary"
        source_text = sanitize_text(naming.render(diary.content, nickname) or "")
    else:
        gate = token_cfg.get("diary_min_user_chars", settings.diary_min_user_chars)
        user_chars = sum(len(m.content or "") for m in user_msgs)
        if user_chars >= gate or "transcript" not in cfg.sources:
            await _delete_row(session, profile.id)
            return "skipped"
        source_kind = "transcript"
        source_text = _transcript_for_push(messages, nickname, language)

    if not source_text.strip():
        await _delete_row(session, profile.id)
        return "skipped"

    # 검수: 결정적 필터 AND 인명 휴리스틱 AND 검수 LLM — 전부 통과해야 저장(fail-closed).
    # 결정적 검사 우선(실패 시 검수 LLM 콜 절약). 리젝은 재생성 재시도로 흡수(GEN_ATTEMPTS)
    # — 개인화 커버리지 요구(디폴트 폴백 최소화). 극성은 유지: 전 시도 실패 = rejected.
    body = ""
    reason = None
    for attempt in range(GEN_ATTEMPTS):
        body = await _generate_body(
            source_text, language, hint=_RETRY_HINT.get(reason) if reason else None
        )
        reason = None
        if not passes_deterministic_filter(body, language):
            reason = "filter"
        elif has_person_reference(body, language, nickname):
            reason = "person_ref"
        elif not await _verify_body(body, language):
            reason = "verify_llm"
        if reason is None:
            break
        _log.info(
            "push_gen 재시도(user=%s attempt=%d reason=%s len=%d)",
            profile.id, attempt + 1, reason, len(body),
        )
    if reason:
        # 문구 내용은 로그에 남기지 않는다 — 리젝된 문구는 정의상 가장 민감한 부류이고
        # journald 사본은 삭제 계약(장벽 즉시 제거)이 닿지 않는다. 사유·길이만 관측.
        _log.info(
            "push_gen 리젝(user=%s lang=%s source=%s reason=%s len=%d)",
            profile.id, language, source_kind, reason, len(body),
        )
        await _delete_row(session, profile.id)
        return "rejected"

    stored = naming.to_placeholder(body, nickname) or body
    await session.execute(
        text(
            "INSERT INTO push_personalizations "
            "(user_id, anchor_date, send_slot, body, language, source_kind, generated_at, "
            " sent_count, last_sent_on) "
            "VALUES (:u, :a, :s, :b, :l, :k, now(), 0, NULL) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            # SET 전체 명시 + 사이클 리셋(sent_count=0, last_sent_on=NULL) — 리셋이 빠지면
            # 3회 소진 유저가 복귀해도 영구 차단되고 §10 효과 측정이 오염된다.
            "  anchor_date = EXCLUDED.anchor_date, send_slot = EXCLUDED.send_slot, "
            "  body = EXCLUDED.body, language = EXCLUDED.language, "
            "  source_kind = EXCLUDED.source_kind, generated_at = now(), "
            "  sent_count = 0, last_sent_on = NULL"
        ),
        {
            "u": profile.id, "a": target, "s": slot,
            "b": stored, "l": language, "k": source_kind,
        },
    )
    await session.commit()
    return "ok"
