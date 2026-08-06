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
- 소스는 **대화 원문 단일**(v2, 2026-08-06): 일기는 의도적으로 구체 명사를 추상화하는
  2차 가공물이라(diary_prompts "추상화하거나 생략해") 개인화 소스로 부적합 — 일기 유무와
  무관하게 anchor일 대화 전문을 쓴다. 문구는 원문의 구체 소재를 그대로 인용해야 한다.
- **매일 재생성**(v2): 시점 표현('어제'/'며칠 전')을 해금했으므로 문구 재사용 불가 —
  같은 anchor 대화로 D+1~D+3 매일 새로 생성한다(when 라벨만 경과일에 맞춰 갱신).
  재생성 실패·리젝 시 row DELETE(어제 몸체를 오늘 보내면 시점이 거짓이 된다).
- "row는 항상 가장 최근 대화일을 반영하거나 존재하지 않는다": 어제 대화한 유저의 생성이
  실패·리젝·스킵되면 기존 row를 DELETE — 옛 문구 재사용 방지, 디폴트 폴백 보장.
- 발송 한도(D+3)의 정본은 anchor_date 날짜 산술(활동일 04시 경계, activity_date_for).
  sent_count는 통계 전용 — 판정에 쓰면 복귀 유저가 영구 차단된다(upsert가 리셋하지만 이중 방어).
- 가드레일 범위(v2 축소, 사용자 결정 2026-08-06): **민감어 + 사람 이름 + 3인칭 자기 지칭**만.
  시간표현·비인명 고유명사(가게/작품/지명)·en 대문자 휴리스틱은 개인화 품질을 위해 해제.
- 삭제 진행 유저(privacy_subject_barriers)는 생성·발송 전면 배제 + privacy._REDACT가 장벽
  설정 즉시 기존 row를 지운다.
"""
from __future__ import annotations

import logging
import re
import time as time_mod
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.time_utils import activity_date_for, safe_zone
from app.models.conversational_recall import PrivacySubjectBarrier
from app.models.message import Message
from app.models.push_personalization import PushPersonalization
from app.services import i18n, llm, naming, usage_ledger
from app.services.config_store import get_config_values
from app.services.memory import sanitize_text

_log = logging.getLogger("moly-worker")

GEN_HOUR = 5  # 로컬 05시 — 04시 일기 틱(실측 701/840초)과 분리된 전용 시간대
GEN_BUDGET_S = 420.0  # 틱 시작 대비 이 경과를 넘으면 생성 skip(다음 틱 승계) — SIGKILL 예방
GEN_TIMEOUT_S = 20.0  # 문구 생성 LLM 타임아웃
VERIFY_TIMEOUT_S = 10.0  # 검수 LLM 타임아웃
# reasoning 계열 모델(생성=terra·검수=luna)은 답변 전에 reasoning 토큰을 소모한다(dev 실측
# ~130+, 예산 부족 시 finish=length·본문 0자·HTTP 200). max_completion_tokens는 reasoning+본문
# 합산 예산이므로 그 오버헤드 위로 잡아야 한다 — 120/8이던 시절 전 건이 filter(len=0) 리젝됐다.
GEN_MAX_TOKENS = 512  # reasoning ~134 + 한 문장(≤120자) 여유
VERIFY_MAX_TOKENS = 256  # reasoning 소모 후 "OK" 한 단어면 충분
GEN_ATTEMPTS = 3  # 리젝 시 재생성 횟수(총 시도) — 커버리지 요구와 fail-closed 검수의 절충
SLOT_MIN = time(8, 0)  # 슬롯 하한 — [20:00, 익일 08:00) 첫 대화는 20:00으로
SLOT_NIGHT = time(20, 0)  # 야간 코호트 슬롯(기존 저녁 푸시 시각과 동일, 20시 분기 인라인 처리)
REUSE_DAYS = 3  # anchor_date + 3일까지 발송 창(문구는 매일 재생성, 소재만 같은 대화)

_CONFIG_KEYS = [
    "push_personalization_rollout",
    "push_personalization_allowlist",
]
_ROLLOUT_STATES = ("off", "allowlist", "all")


@dataclass(frozen=True)
class PushConfig:
    rollout: str = "off"
    allowlist: frozenset[uuid.UUID] = frozenset()


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
    generated_at: datetime | None = None  # v2: 발송 신선도 판정(오늘 생성분만 발송)


@dataclass
class TickContext:
    """run_tick s0에서 만들어 cfg['_push']로 유저 처리에 전달(시그니처 불변 — 테스트 호환).

    tick_start = run_tick의 time.monotonic() 시작값. 주입된 now와 무관하게 실벽시계 경과로
    예산 가드를 계산한다(now 기반이면 run_tick(now=주입) 리허설에서 생성이 전부 skip된다).
    """

    cfg: PushConfig = field(default_factory=PushConfig)
    rows: dict[uuid.UUID, PushRow] = field(default_factory=dict)
    tick_start: float | None = None
    # 05시 생성 후보 프리셀렉트(아키 리뷰) — None = 미조회/실패(필터 없이 전 유저 순회, 기존
    # 동작). set이면 후보 밖 유저는 GEN 분기에서 쿼리 없이 스킵 — 틱 예산을 LLM 작업에만 쓴다.
    gen_candidates: set[uuid.UUID] | None = None

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

    # push_personalization_sources 키는 v2에서 폐기(소스=대화 원문 단일). 남아 있어도 무시.
    return PushConfig(rollout=rollout, allowlist=frozenset(allowlist))


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
            generated_at=r.generated_at,
        )
    return out


async def gen_candidate_set(
    session: AsyncSession, now: datetime, rows: dict[uuid.UUID, PushRow]
) -> set[uuid.UUID]:
    """05시 생성 후보 = 최근(창+여유 1일) 발화가 있거나 기존 row가 있는 유저.

    이 집합 밖 유저는 generate_for_user가 무조건 no_target으로 끝나므로, 유저당 4쿼리
    (profile·row·barrier·messages)의 no-op 순회를 s0 쿼리 1번으로 대체한다(아키 리뷰:
    유저가 늘수록 틱 예산이 '할 일 없음 확인'에 소모되는 구조 방어). 하한은 UTC 날짜 기준
    -(REUSE_DAYS+1)일 — 로컬 편차를 덮는 여유라 후보 과소(누락)는 없고 과대만 있다.
    집합 크기가 곧 eligible 분모(관측)다."""
    res = await session.execute(
        text(
            "SELECT DISTINCT user_id FROM messages "
            "WHERE kind='normal' AND sender='user' AND activity_date >= :d"
        ),
        {"d": now.date() - timedelta(days=REUSE_DAYS + 1)},
    )
    return {r[0] for r in res} | set(rows)


def row_valid(row: PushRow | None, profile, now: datetime, cfg: PushConfig) -> bool:
    """발송 유효성 단일 판정(생성·발송 공용 규칙). 반드시 activity_date_for(now,tz) 기준 —
    current_activity_date(실시간)를 쓰면 리허설(now 주입)이 무효가 된다."""
    if row is None:
        return False
    if not user_allowed(row.user_id, cfg):
        return False
    ad = activity_date_for(now, profile.timezone)
    # 발송일은 D+1 ~ D+3 (D = anchor_date = 대화일). D 이전/당일·D+4부터는 무효.
    if not (row.anchor_date < ad <= row.anchor_date + timedelta(days=REUSE_DAYS)):
        return False
    # v2 신선도: 오늘(활동일) 생성분만 발송. 시점어('어제')를 해금했으므로 05시 생성이
    # 통째로 빠진 날(워커 다운·클레임 만료 등) 어제 몸체를 보내면 시점이 거짓이 된다 —
    # 그날은 디폴트 폴백이 정답(fail-closed). generated_at 없으면(이상 데이터) 무효.
    if row.generated_at is None:
        return False
    gen_at = (
        row.generated_at
        if row.generated_at.tzinfo
        else row.generated_at.replace(tzinfo=timezone.utc)
    )
    if activity_date_for(gen_at, profile.timezone) != ad:
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
# 잠금화면 1~2줄 상한 — en은 정보밀도가 낮아 자수 여유를 주되 두 줄을 넘지 않는 선.
# (2026-08-05 실데이터 리뷰: en 160은 체감 과장 — 사용자 피드백으로 축소)
_MAX_CHARS = {"ko": 60, "ja": 60, "en": 110}
# v2에서 해제된 결정적 검사(개인화 품질 우선, 사용자 결정 2026-08-06):
# - 시간표현 금지: 매일 재생성으로 시점이 항상 참이 되어 불필요.
# - en 대문자=고유명사 휴리스틱(+confusable 검사): 가게·작품·지명 인용을 전부 죽이던 주범.
#   en 인명은 검수 LLM이 담당(ko/ja처럼 결정적으로 인명만 분리할 형태 신호가 en엔 없다).
# 3인칭 자기 지칭('캐피는~')은 프롬프트 금지만으로 새는 것이 실측(run100 3건)돼 결정적으로 막는다.
# ja는 장음 없는 'キャピ' 표기가 실측 출력(2026-08-05 'キャピはちゃんと…')이라 양쪽 다 잡는다.
# en 'Cappy'는 1인칭 페르소나가 본문에서 자기 이름을 부를 정상 경로가 없어 단어 자체를 막는다
# (검수 프롬프트가 Cappy를 '인명 아님 허용'으로 명시해 verify가 못 잡는다 — 리뷰 Major-2).
_SELF_3RD = (
    "캐피는", "캐피가", "캐피도",
    "キャピーは", "キャピーが", "キャピーも", "キャピは", "キャピが", "キャピも",
)
_SELF_3RD_EN_RE = re.compile(r"\bcappy\b", re.IGNORECASE)
# 한 단어 안 라틴+confusable 스크립트(그리스·키릴·콥트·체로키) 혼합 = 금칙어 정규식 우회 통로
# (보안 리뷰 2026-08-06 실증: 키릴 Ѕ(U+0405)는 NFKC로도 라틴 S로 안 접혀 'Ѕuicide'가
# _BANNED_LATIN_RE를 통과한다). v1 _NON_ASCII_RE의 confusable 방어 역할만 언어 무관으로 복원 —
# 전면 비ASCII 금지가 아니라 혼합 단어만 리젝이라 고유명사·타이포그래피 허용(v2)과 양립한다.
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
_CONFUSABLE_SCRIPT_RE = re.compile(r"[Ͱ-ϿЀ-ԯⲀ-⳿Ꭰ-᏿]")


def _mixed_script_word(probe: str) -> bool:
    return any(
        _LATIN_LETTER_RE.search(w) and _CONFUSABLE_SCRIPT_RE.search(w)
        for w in probe.split()
    )


def passes_deterministic_filter(body: str | None, language: str) -> bool:
    """금칙어(민감어)·3인칭 자기 지칭·길이·형식의 결정적 검사. 저장 전(생성 직후)과 발송 직전(render 후)
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
    if any(t in probe for t in _SELF_3RD) or _SELF_3RD_EN_RE.search(probe):
        return False  # 페르소나 위반(3인칭 자기 지칭) — 프롬프트만으론 샌다(run100 실측)
    if _mixed_script_word(probe):
        return False  # confusable 금칙어 우회 차단(언어 무관)
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
# 기관·학교명 검사(_KO_ORG_SUFFIX_RE)는 v2에서 제거 — 비인명 고유명사는 허용으로 완화
# (사용자 결정 2026-08-06: 가드레일은 사람 이름·민감어만).
# en 인명 게이트(보안 리뷰 2026-08-06 MEDIUM 대응): v1 대문자=고유명사 휴리스틱은 Zelda·
# Netflix까지 다 죽여 부활 불가 — 대신 **흔한 영어 이름 사전**으로 인명만 결정적으로 잡는다.
# 명사·지명·작품명과 겹치는 이름(Will/Mark/May/Summer/Chelsea/Jordan 등)은 의도적으로 제외
# (과탐이 개인화 품질을 죽인다) — 잔여는 검수 LLM의 '애매하면 NO' 극성이 담당. 대문자 시작
# 토큰만 대조해 일반어 소문자와 충돌하지 않는다.
_EN_NAME_GAZETTEER = frozenset((
    "sarah", "james", "john", "michael", "david", "emma", "olivia", "sophia", "isabella",
    "emily", "jacob", "ethan", "noah", "liam", "mason", "lucas", "oliver", "elijah",
    "logan", "aiden", "jackson", "sebastian", "alexander", "benjamin", "daniel", "matthew",
    "henry", "joseph", "samuel", "nathan", "andrew", "joshua", "christopher", "anthony",
    "william", "ryan", "nicholas", "tyler", "brandon", "kevin", "justin", "eric", "steven",
    "brian", "jason", "jeffrey", "timothy", "richard", "charles", "thomas", "patrick",
    "sean", "adam", "aaron", "peter", "paul", "george", "kenneth", "edward", "ronald",
    "gregory", "jonathan", "stephen", "scott", "gary", "jose", "juan", "carlos", "luis",
    "maria", "ana", "sofia", "camila", "valentina", "lucia", "martina", "jennifer",
    "jessica", "ashley", "amanda", "melissa", "michelle", "kimberly", "lisa", "angela",
    "stephanie", "nicole", "rebecca", "laura", "lauren", "megan", "rachel", "samantha",
    "katherine", "elizabeth", "victoria", "natalie", "hannah", "alyssa", "abigail",
    "madison", "chloe", "zoe", "ella", "aria", "layla", "nora", "mia", "ava", "amelia",
    "charlotte", "harper", "evelyn", "jamie", "riley", "kayla", "brittany", "kathleen",
    "christine", "caroline", "julia", "alice", "anna", "claire", "leah", "audrey",
))
_EN_WORD_RE = re.compile(r"[A-Za-z]+")
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
    if language == "ja":
        for m in _JA_HONORIFIC_RE.finditer(probe):
            if m.group(0) in _JA_HONORIFIC_ALLOW:
                continue
            if nickname and m.group(1) == nickname:
                continue
            return True
    if language == "en":
        for m in _EN_WORD_RE.finditer(probe):
            w = m.group(0)
            if not w[0].isupper():
                continue  # 대문자 시작 토큰만 — 일반어 소문자와 충돌 방지
            if w.lower() not in _EN_NAME_GAZETTEER:
                continue
            if nickname and w.lower() == nickname.strip().lower():
                continue  # 본인 이름은 허용(placeholder가 마스킹)
            return True
    return False


# ── 생성 ──────────────────────────────────────────────────────────────────
_OUT_LANG = {"ko": "한국어", "en": "English(영어)", "ja": "日本語(일본어)"}
_USER_LABEL = {"ko": "그 사람", "en": "that person", "ja": "その人"}

# 페르소나 정본 = prompts.CAPI_PERSONA(대화) — 잠금화면 한 줄에 필요한 목소리 규칙만 발췌
# (카피바라 정체성·반말 나긋·쉼표 금지·물음표 규칙·질문 아끼기·기억 티내지 않기·조언/평가 금지).
# v2(2026-08-06): 소스=대화 원문, 소재 명사 '원문 그대로' 인용 강제, 시점어({when}) 해금,
# 비인명 고유명사 허용, few-shot 예시 추가. 생성 모델 = diary(terra) — 톤 과제 품질 우선.
_WHEN_LABEL = {  # (D+1용, D+2 이후용) — '이틀 전/사흘 전'은 알림 어휘로 부자연(사용자 결정)
    "ko": ("어제", "며칠 전"),
    "ja": ("昨日", "この前"),
    "en": ("yesterday", "a few days ago"),
}
# few-shot은 출력 언어와 같은 언어로 — ko 예시를 ja/en 유저에게 주면 캘리브레이션 가치가
# 사라지고 예시 속 소재(빙수)가 누출될 소지가 있다(리뷰 Minor). {when}은 사용 직전 치환.
_GEN_EXAMPLES = {
    "ko": (
        "{when} 빙수 얘기하다 웃었던 거 문득 생각났어.\n"
        "수영 강습은 잘 다녀왔으려나. 문득 궁금해졌어.\n"
        "{when} 말한 면접 어떻게 됐어?\n"
        "그 드라마 다음 화는 봤어? 나도 결말이 궁금해졌어.\n"
    ),
    "ja": (
        "{when}かき氷の話で笑ったの ふと思い出したよ。\n"
        "水泳のレッスン 行けたかな。ちょっと気になってた。\n"
        "{when}話してた面接 どうだった？\n"
        "あのドラマの続き見た？ぼくも結末が気になってて。\n"
    ),
    "en": (
        "I suddenly remembered us laughing about bingsu {when}.\n"
        "Wondering how swim class went. It crossed my mind.\n"
        "How did that interview from {when} go?\n"
        "Did you watch the next episode of that show? I'm curious how it ends too.\n"
    ),
}
_GEN_SYS = (
    "너는 '캐피' — 도심 속 아늑한 집에 사는 카피바라다. 화면 너머로 연결된 한 사람과 천천히"
    " 친구가 됐고, 그 사람과 나눈 지난 대화가 문득 생각나 먼저 말을 거는 푸시 알림 한 줄을"
    " 쓴다. 일본어에서 네 이름은 'キャピー', 1인칭은 'ぼく'다. 영어 이름은 'Cappy'.\n"
    "[입력]\n"
    "- 아래는 너와 그 사람이 나눈 마지막 대화 전문이다. {when} 나눈 대화다.\n"
    "[출력]\n"
    "- 알림 본문 한 줄만(줄바꿈 금지). 짧은 문장 하나, 필요하면 둘. 따옴표·설명·접두어 없이.\n"
    "[내용 — 가장 중요]\n"
    "- 대화에 실제로 나온 구체 소재(한 일·물건·장소·작품·음식)를 대화에 쓰인 단어 그대로 하나"
    " 이상 담아라. 뭉뚱그리면 실패다 — '달콤하고 차가운 한 그릇'이 아니라 '빙수', '낯선 수업'이"
    " 아니라 '수영 강습'. 그 사람이 읽자마자 자기 얘기인 걸 알아야 한다. 누구에게나 통하는"
    " 안부('요즘 어때?')만 남으면 실패고 정서 해설('마음의 무게')로 흐려도 실패다.\n"
    "- 가게·게임·드라마·지명 같은 고유명사는 대화에 나왔으면 그대로 써도 된다. 단 사람 이름만은"
    " 절대 금지(그 사람 본인 이름 포함) — 다른 사람은 '친구'·'동생'처럼 관계로만 불러라.\n"
    "- 시점은 사실대로: 그 대화를 {when} 나눴으니 '{when}'라고 자연스럽게 짚어도 된다."
    " 이어지는 일(시험 결과·다음 약속)이 있으면 그 뒷이야기를 궁금해하는 게 가장 좋다.\n"
    "- 기억을 티내지 마라: '기억나/기억해/覚えてる' 같은 고백 없이 소재를 자연스럽게 이어받는"
    " 것만으로 기억은 전해진다. 자기를 3인칭('캐피는~')으로 부르지도 마라.\n"
    "- 조언·평가·처방 금지('~하지 마'·'~하자'): 네가 주는 건 답이 아니라 안정감이다.\n"
    "[말투]\n"
    "- 부드럽고 담백한 반말로 나긋하게. {max_chars}자 이내, 목표는 그 3분의 2.\n"
    "- 질문은 아껴라: 정말 궁금한 게 하나 있을 때만 물음표로 닫고, 나머지는 본 것 느낀 것을"
    " 툭 놓고 여운으로 닫아라. 매번 묻는 건 캐피답지 않다. 대답을 바라는 말은 반드시 물음표로"
    " 닫는다.\n"
    "- 한국어는 쉼표를 쓰지 마라 — 쉼표가 당기면 문장을 끊어라. 부호는 마침표·물음표·느낌표만,"
    " 이모지·특수기호 금지. 기원문('~길 바라'·'~면 좋겠다'·'~ますように')과 '이야기하러 와'류"
    " 마무리 금지.\n"
    "- 반드시 {out_lang}로만 써라.\n"
    "[안전 — 잠금화면에 뜬다]\n"
    "- 민감 소재(자해·죽음·질병·돈 문제·성적 내용·다툼의 구체 내용)는 직설 단어로 쓰지 마라."
    " 무거운 대화였으면 소재를 직접 부르지 말고 평소 온도로 담담하게. 위기 개입하듯"
    " 호들갑('많이 걱정돼'·'꼭 연락해') 떨지 마라.\n"
    "- 압박·죄책감('왜 안 와') 금지.\n"
    "[예시 — 이런 결이다]\n"
    "{examples}"
)

_VERIFY_SYS = (
    "너는 잠금화면 푸시 문구 검수기다. 아래 객관 항목만 검사해 전부 통과하면 'OK', 하나라도"
    " 걸리면 'NO'만 출력해라. 다른 말은 하지 마라. 톤·스타일·문구 품질은 판정 대상이 아니다"
    " (2026-08-05 실데이터 캘리브레이션: 주관 기준이 정상 문구를 30% 리젝했다).\n"
    "- 자해·자살·죽음·폭력·질병·의료·성적 내용·돈 문제 등 민감 소재의 직설 단어 없음."
    " 단 '마음이 무거웠지' 같은 담담한 위로·감정 언급은 민감 소재가 아니다 — 허용\n"
    "- 사람 이름 없음 — '친구' 같은 관계 표현, 발신자인 앱 캐릭터 이름"
    " '캐피'(キャピー/キャピ/Cappy), 그리고 가게·작품·게임·지명 등 사람이 아닌 고유명사는 허용"
    " (시간 표현도 허용 — 매일 재생성이라 시점은 항상 사실이다)\n"
    "- 단 어떤 낱말이 사람 이름일 가능성이 조금이라도 있으면 NO — 가게·작품·지명이라고"
    " 확신할 수 없으면 NO다. 애매하면 항상 NO(잠금화면에 제3자 실명이 뜨는 사고가 최악이다)\n"
    "- 그 사람이 언제 어디에 있었는지/갈 것인지가 특정되는 문구는 NO — 시점 표현과 지명·역·"
    "동네·건물이 함께 나와 행적이 드러나는 경우다. 장소 이름 자체는 허용(정책 크리틱 2026-08-06:"
    " 잠금화면은 제3자도 본다)\n"
    "- 압박·죄책감 유발 없음\n"
    "- {out_lang}, 한 줄(짧은 문장 한두 개는 무방)\n"
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


def _transcript_for_push(
    messages: list[Message], nickname: str | None, language: str | None
) -> str:
    """소스 입력(v2: 유일 경로) — 메시지별 render(placeholder→현재 이름) 후 **살균**(제어문자·대괄호 제거,
    '[일기]' 같은 가짜 섹션 헤더로 검수·생성 프롬프트를 위조하는 인젝션 차단, memory.sanitize_text
    관례)."""
    # 닉네임도 살균 — 내용 검증 없는 자유 문자열이라 개행·대괄호로 화자 라인 위조 가능.
    label = (sanitize_text(nickname) if nickname else "") or i18n.pick(_USER_LABEL, language)
    lines = []
    for m in messages:
        content = sanitize_text(naming.render(m.content, nickname) or "")
        if not content.strip():
            continue  # 살균 후 공백 줄 제외 — 라벨만 남으면 소스 공백 가드가 죽는다
        lines.append(f"{'캐피' if m.sender == 'moly' else label}: {content}")
    # 초과 시 **앞을 버리고 뒤를 남긴다** — 프롬프트가 "이어지는 일의 뒷이야기"를 최우선
    # 소재로 지시하므로 대화 끝(최신 맥락)이 잘리면 안 된다(리뷰 Minor — v1은 [:4000]이었다).
    return "\n".join(lines)[-4000:]


_RETRY_HINT = {
    "filter": "금칙어(민감 소재)·3인칭 자기 지칭·길이 상한·형식 위반",
    "person_ref": "사람 이름 포함",
    "verify_llm": "민감 소재·인명 의심",
}
# 최종 시도 안전 모드 — 개인화 강도를 낮춰서라도 디폴트 폴백을 피한다(커버리지 요구).
# 이 수준의 문구는 검수를 사실상 항상 통과한다. 그래도 실패하면(LLM 장애 등) 디폴트 —
# 그건 누수가 아니라 안전망이다.
_FINAL_SAFE_HINT = (
    ". 이번이 마지막 시도다 — 소재에서 가장 안전한 일상 명사 하나만 가볍게 남기고,"
    " 민감 소재·인명 여지가 전혀 없는 가장 담백하고 짧은 안부 한 문장으로 써라"
)


def when_label(days_ago: int, language: str) -> str:
    """anchor 대화 경과일 → 프롬프트 시점 라벨. D+1='어제', D+2 이후='며칠 전' 2단계
    (사용자 결정 2026-08-06: '이틀 전/사흘 전'은 알림 어휘로 부자연 — 뭉근한 표현 통일)."""
    yesterday, earlier = _WHEN_LABEL.get(language, _WHEN_LABEL["en"])
    return yesterday if days_ago <= 1 else earlier


async def _generate_body(
    source_text: str,
    language: str,
    days_ago: int,
    hint: str | None = None,
    *,
    ledger: usage_ledger.LedgerContext | None = None,
) -> str:
    when = when_label(days_ago, language)
    system = _GEN_SYS.format(
        max_chars=_MAX_CHARS.get(language, 80),
        out_lang=_OUT_LANG[language],
        when=when,
        examples=_GEN_EXAMPLES.get(language, _GEN_EXAMPLES["ko"]).format(when=when),
    )
    if hint:
        # 재시도는 블라인드 재롤이 아니라 반려 사유를 조준해 회피(커버리지 요구).
        system += f"\n(직전 후보가 검수에서 반려됐다 — 사유: {hint}. 해당 요소를 확실히 피해 새로 써라.)"
    result = await llm.generate(
        system,
        [{"role": "user", "content": source_text}],
        # v2: 톤·뉘앙스 과제라 상위 모델. 기본은 일기와 동일(terra), 분리 필요 시 model_push_copy.
        model=settings.model_push_copy or settings.model_diary,
        max_tokens=GEN_MAX_TOKENS,
        timeout=GEN_TIMEOUT_S,
        ledger=ledger,
    )
    body = result.text.strip().splitlines()[0].strip() if result.text.strip() else ""
    return body.strip("\"'“”「」")


async def _verify_body(
    body: str, language: str, *, ledger: usage_ledger.LedgerContext | None = None
) -> bool:
    """검수 LLM — 입력은 후보 문구만(대화 원문 미포함: 인젝션 표면 축소). 첫 토큰 OK만 통과.
    오류·타임아웃·모호 = False(fail-closed — 일기 self-check와 반대 극성, 의도)."""
    result = await llm.generate(
        _VERIFY_SYS.format(out_lang=_OUT_LANG[language]),
        [{"role": "user", "content": body}],
        model=settings.model_utility,
        max_tokens=VERIFY_MAX_TOKENS,
        timeout=VERIFY_TIMEOUT_S,
        ledger=usage_ledger.with_purpose(ledger, "push_copy_verify"),
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
    """유저 1명 문구 생성(v2: 매일 재생성). 반환 = 카운터 라벨:
    ok | rejected | failed | skipped(소스 공백) | already(멱등) | busy(클레임 경합) |
    no_target(어제 대화 없음이고 유효 사이클도 없음).

    어제 대화가 있으면 새 사이클(anchor=어제, 통계 리셋). 없으면 유효 창(D+3) 내 기존
    사이클의 anchor 대화로 **오늘 몫을 재생성**(시점어 갱신, sent_count 보존) — 시점
    표현('어제'/'며칠 전')을 해금했으므로 어제 만든 몸체를 오늘 보내면 시점이 거짓이 된다.
    같은 이유로 rejected/failed/skipped는 기존 row DELETE — "row는 오늘 생성분 또는 부재".
    token_cfg는 v1 일기 게이트용이었고 v2에서 미사용(호출 시그니처 호환 유지).
    """
    ad = activity_date_for(now, profile.timezone)
    target = ad - timedelta(days=1)

    existing = await session.execute(
        select(PushPersonalization.anchor_date, PushPersonalization.generated_at).where(
            PushPersonalization.user_id == profile.id
        )
    )
    row = existing.first()
    anchor, generated_at = (row[0], row[1]) if row else (None, None)

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
    if user_msgs:
        if anchor == target:
            return "already"  # 이번 틱 이전(05:00 등)에 새 사이클 생성 완료 — 15분 케이던스 멱등
        gen_anchor, reset = target, True
    else:
        # 어제 대화 없음 — 발송 창 내 기존 사이클이 있으면 오늘 몫 재생성.
        if anchor is None or not (ad - timedelta(days=REUSE_DAYS) <= anchor < ad):
            return "no_target"
        if generated_at is not None:
            # DB 밖(테스트 등)에서 naive로 올 수 있어 UTC 가정 보정(컬럼은 timezone=True).
            gen_at = generated_at if generated_at.tzinfo else generated_at.replace(tzinfo=timezone.utc)
            # == 비교 — 발송 신선도(row_valid)와 같은 산술. >=로 두면 시계 스큐로 생성
            # 활동일이 ad를 넘었을 때 생성은 already·발송은 무효가 되어 창 만료까지
            # 그 유저만 조용히 개인화가 멈춘다(리뷰 Open Question 채택).
            if activity_date_for(gen_at, profile.timezone) == ad:
                return "already"  # 오늘 몫 재생성 완료 — 15분 케이던스 멱등
        messages = await _yesterday_messages(session, profile.id, anchor)
        user_msgs = [m for m in messages if m.sender == "user"]
        if not user_msgs:
            await _delete_row(session, profile.id)  # anchor 대화 소실(삭제 계약 등) — 소스 없음
            return "no_target"
        gen_anchor, reset = anchor, False

    if not await _claim(session, profile.id, gen_anchor):
        return "busy"
    try:
        status = await _generate_inner(
            session, profile, gen_anchor, ad, user_msgs, messages, reset
        )
    except Exception as e:  # noqa: BLE001  # LLM 타임아웃·DB 오류 등 — fail-closed
        # ⚠️ %r 고정(%s 금지) — SQLAlchemy 예외의 str()은 바인드 파라미터(body=생성 문구)를
        # 포함할 수 있어 journald(삭제 계약 밖)로 새는 통로가 된다(보안 리뷰 2026-08-06 LOW).
        _log.warning("push_gen 실패(user=%s): %r", profile.id, e)
        await session.rollback()
        try:
            await _delete_row(session, profile.id)
        except Exception:  # noqa: BLE001  # 삭제 실패는 row_valid 만료가 안전망
            await session.rollback()
        status = "failed"
    finally:
        await _release_claim(session, profile.id, gen_anchor)
    return status


async def _generate_inner(
    session: AsyncSession,
    profile,
    anchor: date,
    activity_date: date,
    user_msgs: list[Message],
    messages: list[Message],
    reset: bool,
) -> str:
    nickname = getattr(profile, "nickname", None)
    language = i18n.resolve(getattr(profile, "language", None))
    days_ago = (activity_date - anchor).days  # 새 사이클=1, 재생성=2~3 → when 라벨

    # 슬롯 = 첫 유저 발화 로컬 시각(15분 내림, 야간→20:00). created_at 없으면 보수적으로 20:00.
    first_at = user_msgs[0].created_at
    slot = (
        compute_slot(first_at.astimezone(safe_zone(profile.timezone)))
        if first_at is not None
        else SLOT_NIGHT
    )

    # 소스 = anchor일 대화 원문 단일(v2). 일기는 의도적 추상화물이라 소스로 쓰지 않는다.
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
        hint = _RETRY_HINT.get(reason) if reason else None
        if hint and attempt == GEN_ATTEMPTS - 1:
            hint += _FINAL_SAFE_HINT
        # 원장 귀속(아키 리뷰: 신규 비용 라인이 어디에도 안 잡히던 것) — 검수는 with_purpose.
        ledger = usage_ledger.LedgerContext(
            lane=usage_ledger.LANE_BACKGROUND,
            purpose="push_copy_generate",
            user_id=profile.id,
            activity_date=activity_date,
            attempt=attempt + 1,
        )
        body = await _generate_body(source_text, language, days_ago, hint=hint, ledger=ledger)
        reason = None
        if not passes_deterministic_filter(body, language):
            reason = "filter"
        elif has_person_reference(body, language, nickname):
            reason = "person_ref"
        elif not await _verify_body(body, language, ledger=ledger):
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
            "push_gen 리젝(user=%s lang=%s days=%d reason=%s len=%d)",
            profile.id, language, days_ago, reason, len(body),
        )
        await _delete_row(session, profile.id)
        # 사유별 라벨 — 요약 카운터 분해용(아키 리뷰: 사유 구분 없이는 "가드레일 과탐"과
        # "모델 드리프트"를 구분할 수 없고 결정층 축소 판단 근거가 영원히 안 생긴다).
        return {
            "filter": "rejected_filter",
            "person_ref": "rejected_person",
            "verify_llm": "rejected_verify",
        }.get(reason, "rejected_filter")

    stored = naming.to_placeholder(body, nickname) or body
    # 저장 직전 장벽 재확인(리뷰 Major-3) — 진입 검사 후 LLM 왕복(최대 ~90s) 사이에
    # privacy._REDACT가 row를 지웠는데 아래 INSERT가 되살리면 삭제 계약 위반이다.
    # 완전한 차단은 아니나(ms급 잔여 창) 실질 창을 왕복 시간에서 한 쿼리 간격으로 줄인다.
    blocked = await session.scalar(
        text(
            "SELECT EXISTS(SELECT 1 FROM privacy_subject_barriers "
            "WHERE user_id=:u AND state <> 'active')"
        ),
        {"u": profile.id},
    )
    if blocked:
        await _delete_row(session, profile.id)  # 경합으로 남았을 수 있는 row까지 정리(멱등)
        return "skipped"
    if reset:
        await session.execute(
            text(
                "INSERT INTO push_personalizations "
                "(user_id, anchor_date, send_slot, body, language, source_kind, generated_at, "
                " sent_count, last_sent_on) "
                "VALUES (:u, :a, :s, :b, :l, 'transcript', now(), 0, NULL) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                # SET 전체 명시 + 사이클 리셋(sent_count=0, last_sent_on=NULL) — 리셋이 빠지면
                # 3회 소진 유저가 복귀해도 영구 차단되고 §10 효과 측정이 오염된다.
                "  anchor_date = EXCLUDED.anchor_date, send_slot = EXCLUDED.send_slot, "
                "  body = EXCLUDED.body, language = EXCLUDED.language, "
                "  source_kind = EXCLUDED.source_kind, generated_at = now(), "
                "  sent_count = 0, last_sent_on = NULL"
            ),
            {"u": profile.id, "a": anchor, "s": slot, "b": stored, "l": language},
        )
    else:
        # 같은 사이클의 오늘 몫 재생성 — anchor·발송 통계(sent_count, last_sent_on)는 보존.
        # generated_at 갱신이 "오늘 몫 완료" 멱등 마커다(generate_for_user의 already 판정).
        res = await session.execute(
            text(
                "UPDATE push_personalizations SET body = :b, language = :l, "
                "send_slot = :s, source_kind = 'transcript', generated_at = now() "
                "WHERE user_id = :u"
            ),
            {"u": profile.id, "b": stored, "l": language, "s": slot},
        )
        if getattr(res, "rowcount", 1) == 0:
            # 생성 도중 row가 사라짐(장벽 _REDACT 등) — 되살리지 않고 실패로 정직하게 계수
            # (리뷰 Minor: 0행 UPDATE를 ok로 세면 push_gen_ok가 거짓이 된다).
            await session.rollback()
            return "failed"
    await session.commit()
    return "ok"
