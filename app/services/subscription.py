"""구독 — RevenueCat이 진실 소스. 상태 조회 + RC 웹훅으로 상태·혜택 동기(서버 권위).

RC가 Apple/Google 영수증 검증을 대행 → 우리는 RC 웹훅 이벤트만 신뢰(서명 대신 웹훅 인증).
증정 = 플랜별 최초 1회(월1000/연4000, DB UNIQUE 강제). 건초 IAP = NON_RENEWING_PURCHASE.

SOMA-372: 웹훅은 내구 inbox(revenuecat_events)에 raw 커밋 후 process_event로 소비한다.
- 큰문제1(순서 역행 오만료): 상태 단조(event_ts > last_event_at) + 단조 연장 예외.
- 큰문제2(오류 삼킴 유실): 핸들러 내부 commit·광범위 except 제거 → 예외는 롤백·재시도, 은폐 없음.
- 큰문제3(환불 회수 우회): 음수 부채 회수(실 원장액 권위) + 팩 환불 경로 + TRANSFER 전면 수동.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import case, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pg import unique_violation
from app.models.hay_transaction import HayTransaction
from app.models.order import Order
from app.models.payment import Payment
from app.models.revenuecat_event import RevenuecatEvent
from app.models.subscription import Subscription
from app.models.subscription_hay_grant import SubscriptionHayGrant
from app.services import hay_ledger, i18n, payment
from app.services.account import _load_profile
from app.services.config_store import get_config_values
from app.services.entitlement import _parse_dt
from app.services.limits import effective_token_config

_log = logging.getLogger("moly-backend")

# 프로덕션 pg_constraint 확인(배포 전 재확인): 인라인 UNIQUE는 Postgres 자동명 규칙.
_SUB_OTX_UQ = "subscriptions_original_transaction_id_key"
_GRANT_UQ = "subscription_hay_grants_user_plan_uq"
_PAYMENT_TX_UQ = "payments_store_transaction_id_key"
_MAX_ATTEMPTS = 5  # 예상 밖 예외 재시도 상한(도달 시 failed → 수동 처리)
_RETRY_BACKOFF = timedelta(minutes=5)  # 재-pending 재시도 지연 — dependency 내부 rotation·과도 재처리 방지(next_attempt_at)
_TXN_ID_MAX = 512  # 외부 거래ID 상식적 상한(초과=permanent_failure). 저장은 원문(text) — truncate 금지
_ENV_CAP = 32      # environment 등 외부 짧은 문자열 저장 상한

HAY_GRANT = {"monthly": 1000, "yearly": 4000}
_VALID_PLANS = frozenset(HAY_GRANT)  # 내부 요금제 화이트리스트(config 오값 방어)
# 구독 상품 카탈로그(클라 노출·매핑 단일 소스). Apple 상품ID는 확정(코드 소스).
_PLANS = [
    {"product_id": "app.moly.sub.monthly", "period": "monthly", "hay_grant": 1000},
    {"product_id": "app.moly.sub.yearly", "period": "yearly", "hay_grant": 4000},
]
# Apple 상품ID → 내부 요금제. _PLANS에서 파생(단일 소스).
_APPLE_PRODUCTS = {p["product_id"]: p["period"] for p in _PLANS}
# Google Play 상품ID → 내부 요금제. Play Console 확정 후 app_config로 주입(코드 재배포 없이).
# 형식: {"<구독ID>[:<basePlanId>]": "monthly"|"yearly"}. 미설정 시 Google 구독 이벤트는
# "미등록 상품"으로 관측(혜택 미지급). SOMA-341.
_GOOGLE_PRODUCTS_KEY = "google_play_subscription_products"
_BENEFITS = {
    "ko": ["대화 한도 확장", "개인 일기 발행", "배너 광고 제거", "건초 증정"],
    "en": ["Extended chat limit", "Personal diary", "No banner ads", "Hay gift"],
    "ja": ["会話の上限アップ", "あなただけの日記", "バナー広告なし", "干し草プレゼント"],
}
_ACTIVE = ("active", "grace_period")  # 혜택 유지되는 구독 상태


def _ms_to_dt(ms) -> datetime | None:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc) if ms else None


def _capped(value, n: int) -> str | None:
    """외부 문자열 저장 상한 — 과대 입력이 인덱스·내구를 깨지 않게 캡(None은 그대로)."""
    return str(value)[:n] if value else None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


async def get_plans(session: AsyncSession, user_id: str) -> dict[str, Any]:
    """요금제 목록 + 유저 언어의 혜택 문구(구독 안내는 사용자 언어로, SOMA-346)."""
    profile = await _load_profile(session, user_id)
    return {"plans": _PLANS, "benefits": i18n.pick(_BENEFITS, profile.language)}


async def _google_products(session: AsyncSession) -> dict[str, str]:
    """app_config의 Google Play 구독 상품 매핑({상품ID: plan}). 미설정 시 빈 dict."""
    cfg = await get_config_values(session, [_GOOGLE_PRODUCTS_KEY])
    m = cfg.get(_GOOGLE_PRODUCTS_KEY)
    return m if isinstance(m, dict) else {}


async def _resolve_plan(
    session: AsyncSession, etype: str | None, event: dict
) -> tuple[str | None, str | None]:
    """RC 이벤트 상품ID → 내부 요금제(스토어 무관). 반환 = (plan, 유효 상품ID).

    - PRODUCT_CHANGE는 변경 후 상품(new_product_id)을 기준으로 요금제 결정.
    - Apple(코드 소스) 우선 조회 → 없으면 Google(app_config) 조회. Apple 상품이면
      config 조회 없이 반환(핫패스·기존 동작 보존).
    - Google Play 구독은 '구독ID:basePlanId' 형태로 올 수 있어, 전체 일치 실패 시
      ':' 앞 구독ID로 재시도.
    """
    pid = event.get("product_id")
    if etype == "PRODUCT_CHANGE":
        pid = event.get("new_product_id") or pid
    if not pid:
        return None, pid
    plan = _APPLE_PRODUCTS.get(pid)
    if plan is not None:
        return plan, pid
    google = await _google_products(session)
    plan = google.get(pid)
    if plan is None and ":" in pid:  # Google base plan 접미사 정규화
        base = pid.split(":", 1)[0]
        plan = _APPLE_PRODUCTS.get(base) or google.get(base)
    if plan is not None and plan not in _VALID_PLANS:  # config 오타·비정상 값 방어
        _log.warning("RC 웹훅: 유효하지 않은 plan 매핑값(%r) — product=%r", plan, pid)
        plan = None
    return plan, pid


async def _latest_sub(session: AsyncSession, uid) -> Subscription | None:
    return (
        await session.execute(
            select(Subscription).where(Subscription.user_id == uid).order_by(
                Subscription.expires_at.desc().nullslast()
            ).limit(1)
        )
    ).scalars().first()


async def get_subscription(session: AsyncSession, user_id: str) -> dict[str, Any]:
    profile = await _load_profile(session, user_id)
    sub = await _latest_sub(session, profile.id)
    now = datetime.now(timezone.utc)
    # 무료 체험 표시(entitlement와 일관): 활성 구독자=체험 아님 / 런칭 기간=런칭 종료까지 / 아니면 실제 2일.
    active = sub is not None and sub.status in _ACTIVE and sub.expires_at is not None and sub.expires_at > now
    launch_until = _parse_dt((await effective_token_config(session)).get("free_launch_until"))
    if active:
        in_trial, trial_ends = False, None
    elif launch_until is not None and now < launch_until:
        in_trial, trial_ends = True, launch_until
    elif profile.trial_ends_at is not None and now < profile.trial_ends_at:
        in_trial, trial_ends = True, profile.trial_ends_at
    else:
        in_trial, trial_ends = False, None
    if sub is None:
        return {
            "status": "none", "plan": None, "auto_renew_enabled": False, "expires_at": None,
            "in_trial": in_trial, "trial_ends_at": _iso(trial_ends),
        }
    return {
        "status": sub.status, "plan": sub.plan, "auto_renew_enabled": sub.auto_renew_enabled,
        "expires_at": _iso(sub.expires_at), "in_trial": in_trial, "trial_ends_at": _iso(trial_ends),
    }


async def _by_original_tx(
    session: AsyncSession, original_tx: str, *, lock: bool = False
) -> Subscription | None:
    q = select(Subscription).where(Subscription.original_transaction_id == original_tx)
    if lock:  # 웹훅 동시처리 직렬화(REFUND 중복 clawback 레이스 방지)
        q = q.with_for_update()
    return (await session.execute(q)).scalars().first()


async def _grant_exists(session: AsyncSession, uid, plan: str) -> bool:
    row = await session.execute(
        select(SubscriptionHayGrant).where(
            SubscriptionHayGrant.user_id == uid, SubscriptionHayGrant.plan == plan
        )
    )
    return row.scalars().first() is not None


def _event_money(event: dict) -> tuple[Decimal | None, str | None]:
    """RC 이벤트의 실제 결제 금액(원통화 무손실 Decimal)·통화(미확인=None). 구독·IAP 매출 원장 공용.

    금액은 4.99→5 같은 반올림 손실 없이, 통화는 RC 값 그대로(미확인이면 KRW로 날조하지 않는다).
    """
    price = event.get("price_in_purchased_currency")
    try:
        amount = Decimal(str(price)) if price is not None else None
    except (TypeError, ValueError, InvalidOperation):
        amount = None
    currency = event.get("currency")
    return amount, (str(currency)[:10] if currency else None)  # 통화 길이 캡(오염 방어)


# RevenueCat store 값 → payments.store 정규화. 미확인 스토어는 소문자 원값, 누락은 경고.
_RC_STORE_MAP = {
    "APP_STORE": "app_store", "MAC_APP_STORE": "app_store",
    "PLAY_STORE": "play_store", "AMAZON": "amazon",
    "STRIPE": "stripe", "PROMOTIONAL": "promotional",
}


def _store_of(event: dict) -> str:
    raw = str(event.get("store") or "").upper()[:32]  # store 길이 캡(오염 방어)
    if not raw:
        _log.warning("RC 웹훅: store 누락 — 스토어 미상 기록(tx=%r)", event.get("transaction_id"))
        return "unknown"
    mapped = _RC_STORE_MAP.get(raw)
    if mapped is None:  # RC가 새 store 유형 추가 시 감지(소문자 원값으로 기록하되 관측)
        _log.info("RC 웹훅: 미등록 store(%r) — 소문자 원값 기록", raw)
        return raw.lower()
    return mapped


# ─────────────────────────────────────────────────────────────
# 핸들러 결과 분류(silent return 은폐 금지) + inbox 엔진
# ─────────────────────────────────────────────────────────────
HANDLED = "handled"          # 상태·재무 변경 완료 → processed
NO_OP = "no_op"              # 의도적 무처리(상태 단조 skip·PRODUCT_CHANGE 등) → processed + durable reason
DEPENDENCY_MISSING = "dependency_missing"  # 선행 결제/구독 없음 → pending(attempts 불변, 다음 틱 수렴)
PERMANENT_FAILURE = "permanent_failure"    # 재시도로 안 풀리는 영구 오류 → failed(즉시 관측)
TRANSFER_MANUAL = "transfer"               # TRANSFER 전면 수동 → failed + 워커 재요약


@dataclass(frozen=True)
class HandlerResult:
    outcome: str
    reason: str | None = None


class _HandlerSignal(Exception):
    """깊은 헬퍼에서 상위로 결과 분류를 올리는 제어 신호(예상 밖 예외와 구분)."""

    def __init__(self, outcome: str, reason: str) -> None:
        self.outcome = outcome
        self.reason = reason
        super().__init__(reason)


def _permanent(reason: str) -> _HandlerSignal:
    return _HandlerSignal(PERMANENT_FAILURE, reason)


def _dependency(reason: str) -> _HandlerSignal:
    return _HandlerSignal(DEPENDENCY_MISSING, reason)


def _txn_id(value: Any, *, field: str) -> str:
    """외부 거래ID를 원문 그대로 반환(저장·조회가 동일 ID를 쓰도록 통일). 과대 입력(>_TXN_ID_MAX)은
    permanent_failure로 거절 — truncate하면 저장(잘린 ID)과 환불·복구 조회(원문 ID)가 어긋나
    기존 Payment를 영영 못 찾는다(dependency_missing 무한, SOMA-372 회귀 방지)."""
    tx = str(value or "")
    if len(tx) > _TXN_ID_MAX:
        raise _permanent(f"{field} 과대(>{_TXN_ID_MAX}자) len={len(tx)}")
    return tx


async def process_event(session: AsyncSession, event_id: str) -> str:
    """inbox 한 건 처리(엔드포인트·워커 공용). 반환 = 결과 라벨(관측용).

    한 트랜잭션: pending 행을 FOR UPDATE SKIP LOCKED로 claim(0행→반환) → 핸들러 실행.
    'processing' 상태 없음(크래시=롤백→pending). 결과별 트랜잭션 경계:
    - handled/no_op → 같은 트랜잭션에서 inbox=processed 원자 커밋(경제 변경과 함께).
    - dependency_missing/permanent_failure/transfer → 핸들러 tx 롤백(부분 경제변경 폐기) 후
      별도 tx로 상태 기록(status='pending'일 때만 원자적 — 경쟁 처리자 결과 미덮음).
    - 예상 밖 예외 → 롤백 후 별도 tx로 attempts+1·(≥5→failed).
    """
    row = (
        await session.execute(
            select(RevenuecatEvent)
            .where(RevenuecatEvent.event_id == event_id, RevenuecatEvent.status == "pending")
            .with_for_update(skip_locked=True)
        )
    ).scalars().first()
    if row is None:
        return "skipped"  # 이미 처리됐거나 다른 처리자가 잠금 보유
    try:
        result = await handle_revenuecat_event(session, dict(row.payload))
        if result.outcome in (HANDLED, NO_OP):
            row.status = "processed"
            row.processed_at = datetime.now(timezone.utc)
            row.last_error = result.reason  # no-op durable reason(은폐 아님)
            await session.commit()  # 최종 flush/commit도 try 안 — IntegrityError·직렬화 오류 시 재시도
            return result.outcome
    except Exception as exc:  # noqa: BLE001  # 예상 밖(핸들러·commit) — 롤백 후 별도 tx로 재시도
        _log.exception("RC inbox 처리 예외(event=%s): %r", event_id, exc)
        await session.rollback()
        await _record_retry(session, event_id, repr(exc))
        return "exception"
    await session.rollback()  # 부분 경제변경 폐기
    await _record_terminal(session, event_id, result.outcome, result.reason)
    return result.outcome


async def _record_terminal(
    session: AsyncSession, event_id: str, outcome: str, reason: str | None
) -> None:
    """dependency_missing(pending 유지·attempts 불변) / permanent_failure·transfer(failed) 기록.

    status='pending'일 때만 원자적 갱신 — 경쟁 처리자가 이미 전이한 행은 덮지 않는다.
    """
    if outcome == DEPENDENCY_MISSING:
        # next_attempt_at을 뒤로 밀어(backoff) 이 dependency는 다음 틱에서 후순위 → 다른 dependency가
        # 앞으로(집합 내부 rotation·starvation 해소, SOMA-372). attempts는 불변(예외 아님).
        values: dict[str, Any] = {
            "last_error": reason,
            "next_attempt_at": datetime.now(timezone.utc) + _RETRY_BACKOFF,
        }
    else:  # permanent_failure / transfer
        values = {
            "status": "failed", "last_error": reason,
            "processed_at": datetime.now(timezone.utc),
        }
    await session.execute(
        update(RevenuecatEvent)
        .where(RevenuecatEvent.event_id == event_id, RevenuecatEvent.status == "pending")
        .values(**values)
    )
    await session.commit()


async def _record_retry(session: AsyncSession, event_id: str, err: str) -> None:
    """예상 밖 예외 — attempts+1, _MAX_ATTEMPTS 도달 시 failed. status='pending'일 때만 원자적."""
    reached = RevenuecatEvent.attempts + 1 >= _MAX_ATTEMPTS
    await session.execute(
        update(RevenuecatEvent)
        .where(RevenuecatEvent.event_id == event_id, RevenuecatEvent.status == "pending")
        .values(
            attempts=RevenuecatEvent.attempts + 1,
            last_error=err[:4000],
            status=case((reached, "failed"), else_="pending"),
            processed_at=case((reached, datetime.now(timezone.utc)), else_=None),
            next_attempt_at=datetime.now(timezone.utc) + _RETRY_BACKOFF,  # backoff — rotation·과도 재처리 방지
        )
    )
    await session.commit()


async def ingest_event(session: AsyncSession, event_id: str, event: dict) -> str:
    """엔드포인트 진입 — raw를 inbox에 커밋(중복 event.id 멱등) 후 동기 process_event.

    INSERT ON CONFLICT DO NOTHING로 중복 웹훅은 무시(멱등), 커밋으로 durable 보장 후 소비.
    """
    await session.execute(
        pg_insert(RevenuecatEvent)
        .values(event_id=event_id, payload=event)
        .on_conflict_do_nothing(index_elements=["event_id"])
    )
    await session.commit()
    return await process_event(session, event_id)


# ─────────────────────────────────────────────────────────────
# 상태 단조(§3) — status/plan/expires/auto_renew는 event_ts > last_event_at 일 때만
# ─────────────────────────────────────────────────────────────
def _state_applies(sub: Subscription, event_ts: datetime | None) -> bool:
    if event_ts is None:
        return False  # timestamp 누락 → 상태 미적용(보수적·관측)
    if sub.last_event_at is None:
        return True   # 첫 이벤트는 항상 적용
    return event_ts > sub.last_event_at  # 초과만(동일 ts는 no-op)


def _bump_last_event(sub: Subscription, event_ts: datetime | None) -> None:
    """last_event_at = max(existing, event_ts) — 역행 금지(단조 연장 예외 후에도 안전)."""
    if event_ts is None:
        return
    if sub.last_event_at is None or event_ts > sub.last_event_at:
        sub.last_event_at = event_ts


# RC 활성계열(REFUND_REVERSED는 복구라 별도). 상태·재무 갱신 이벤트.
_RC_ACTIVE = frozenset(
    {"INITIAL_PURCHASE", "RENEWAL", "UNCANCELLATION", "PRODUCT_CHANGE", "SUBSCRIPTION_EXTENDED"}
)


async def handle_revenuecat_event(session: AsyncSession, event: dict) -> HandlerResult:
    """RC 웹훅 이벤트 → 구독 상태·혜택 동기(서버 권위). process_event가 트랜잭션 경계로 호출.

    내부 commit·광범위 except 없음 — 결과는 HandlerResult로 분류하고, 예상 밖 예외/기타
    IntegrityError는 상위로 전파(process_event가 롤백·재시도). ⚠️ app_user_id = 우리 Supabase
    user_id 전제(클라가 RC logIn을 우리 uid로 해야 함).
    """
    try:
        return await _dispatch(session, event)
    except _HandlerSignal as sig:
        return HandlerResult(sig.outcome, sig.reason)


async def _dispatch(session: AsyncSession, event: dict) -> HandlerResult:
    etype = event.get("type")

    # TRANSFER는 전면 수동(§5) — 경제·구독·결제 무변경, 즉시 failed + 워커 재요약. uid 파싱 이전에
    # 분기(transferred_from/to만 있고 app_user_id가 없을 수 있음). 무성 회수우회 원천 차단.
    if etype == "TRANSFER":
        return HandlerResult(
            TRANSFER_MANUAL,
            f"TRANSFER 수동 처리 필요 from={event.get('transferred_from')!r} "
            f"to={event.get('transferred_to')!r}",
        )

    try:
        uid = uuid.UUID(str(event.get("app_user_id")))
    except (ValueError, TypeError):
        return HandlerResult(
            PERMANENT_FAILURE, f"app_user_id 파싱 불가: {event.get('app_user_id')!r}"
        )

    event_ts = _ms_to_dt(event.get("event_timestamp_ms"))

    if etype in _RC_ACTIVE:
        return await _handle_active(session, etype, event, uid, event_ts)
    if etype == "REFUND_REVERSED":
        return await _handle_refund_reversed(session, event, event_ts)
    if etype == "CANCELLATION":
        return await _handle_cancellation(session, event, event_ts)
    if etype == "EXPIRATION":
        return await _handle_expiration(session, event, event_ts)
    if etype == "BILLING_ISSUE":
        return await _handle_billing_issue(session, event, event_ts)
    if etype == "NON_RENEWING_PURCHASE":  # 건초 IAP(소비성)
        product_id = str(event.get("product_id") or "")
        transaction_id = _txn_id(event.get("transaction_id"), field="transaction_id")  # 원문 저장·과대 거절
        if not product_id or not transaction_id:  # 거래ID·상품ID 누락 — 은폐 금지(§11.3)
            return HandlerResult(
                PERMANENT_FAILURE,
                f"건초 IAP 식별자 누락 product={product_id!r} tx={transaction_id!r}",
            )
        amount, currency = _event_money(event)  # 해외 결제 실제 통화/금액 기록(구독과 동일)
        store = _store_of(event)
        granted = await payment.grant_pack(
            session, uid, product_id, transaction_id,
            store=store, amount=amount, currency=currency,
        )
        if not granted:  # 미등록 상품 — 건초 미지급이 관측에서 빠지지 않게 permanent_failure
            return HandlerResult(
                PERMANENT_FAILURE, f"미등록 건초 상품 product={product_id!r} store={store}"
            )
        return HandlerResult(HANDLED, None)
    # SUBSCRIPTION_PAUSED(만료 시 처리)·TEST·paywall 등 정보성 — no-op(processed + durable reason).
    return HandlerResult(NO_OP, f"미처리 이벤트 {etype}")


# ─────────────────────────────────────────────────────────────
# 활성계열(구매·갱신·해지취소·상품변경·연장)
# ─────────────────────────────────────────────────────────────
async def _handle_active(
    session: AsyncSession, etype: str, event: dict, uid: uuid.UUID, event_ts: datetime | None
) -> HandlerResult:
    plan, eff_pid = await _resolve_plan(session, etype, event)
    if plan is None:
        return HandlerResult(PERMANENT_FAILURE, f"미등록 구독 상품 type={etype} product={eff_pid!r}")
    original_tx = _txn_id(
        event.get("original_transaction_id") or event.get("transaction_id"),
        field="original_transaction_id",
    )
    if not original_tx:
        return HandlerResult(PERMANENT_FAILURE, f"거래 ID 없음 type={etype}")
    expires = _ms_to_dt(event.get("expiration_at_ms"))
    latest_tx = _txn_id(event.get("transaction_id"), field="transaction_id") or None

    sub = await _by_original_tx(session, original_tx, lock=True)
    if sub is not None and sub.user_id != uid:
        return HandlerResult(PERMANENT_FAILURE, f"다른 계정 소유 구독 {original_tx}")

    if sub is None:
        sub = await _create_or_get_subscription(
            session, uid, plan, original_tx, latest_tx, expires, event, event_ts
        )
    else:
        _apply_active_state(sub, etype, plan, expires, latest_tx, event_ts)

    # 재무는 상태와 분리(옛 이벤트여도 멱등 처리) — 결제 기록·증정은 INITIAL_PURCHASE/RENEWAL만(새 과금).
    if etype in ("INITIAL_PURCHASE", "RENEWAL"):
        await _record_subscription_payment(session, sub, event)
        await _grant_if_first(session, uid, plan)
    return HandlerResult(HANDLED, None)


def _apply_active_state(
    sub: Subscription, etype: str, plan: str, expires: datetime | None,
    latest_tx: str | None, event_ts: datetime | None,
) -> None:
    """상태 단조 + 단조 연장 예외로 status/plan/expires/auto_renew 적용. PRODUCT_CHANGE는 상태·plan 미변경."""
    applies = _state_applies(sub, event_ts)
    # 단조 연장 예외(§3): INITIAL_PURCHASE/RENEWAL이 만료를 앞으로 연장하면 옛 event_ts여도 적용
    # (앞 연장은 손실 아님·replay는 같거나 과거라 무해). ⚠️ revoked(환불됨)면 재활성화 금지.
    extend = (
        etype in ("INITIAL_PURCHASE", "RENEWAL")
        and expires is not None and sub.expires_at is not None
        and expires > sub.expires_at and sub.status != "revoked"
    )
    if not (applies or extend):
        return  # 옛 상태 이벤트 — no-op(재무는 호출측이 별도 멱등 처리)
    if latest_tx:
        sub.latest_transaction_id = latest_tx
    if etype == "PRODUCT_CHANGE":
        # 정보성 — 상태·plan·expires 미변경(누락 Payment 위조·요금제 오변경 방지). last_event_at만 전진.
        _bump_last_event(sub, event_ts)
        return
    sub.plan = plan
    sub.status = "active"
    sub.expires_at = expires
    sub.auto_renew_enabled = True
    _bump_last_event(sub, event_ts)


async def _create_or_get_subscription(
    session: AsyncSession, uid: uuid.UUID, plan: str, original_tx: str,
    latest_tx: str | None, expires: datetime | None, event: dict, event_ts: datetime | None,
) -> Subscription:
    """신규 구독 생성(savepoint) — 동시 처리자가 먼저 만들었으면 그 행에 상태 재적용(멱등 수렴)."""
    sub = Subscription(
        id=uuid.uuid4(), user_id=uid, plan=plan, status="active",
        original_transaction_id=original_tx, latest_transaction_id=latest_tx,
        expires_at=expires, auto_renew_enabled=True,
        environment=_capped(event.get("environment"), _ENV_CAP), last_event_at=event_ts,
    )
    try:
        async with session.begin_nested():
            session.add(sub)
            await session.flush()
        return sub
    except IntegrityError as exc:
        if not unique_violation(exc, _SUB_OTX_UQ):
            raise  # 예상 밖 → 상위 전파(inbox 재시도)
        existing = await _by_original_tx(session, original_tx, lock=True)
        if existing is None:
            raise  # 위반인데 조회 실패 — 예상 밖
        # 기존행 의미 재검증(§6) — 동시 생성 충돌 후 소유자 불일치면 멱등 수렴이 아니라 실오류.
        if existing.user_id != uid:
            raise _permanent(f"다른 계정 소유 구독 {original_tx}")
        _apply_active_state(existing, event.get("type"), plan, expires, latest_tx, event_ts)
        return existing


async def _record_subscription_payment(session: AsyncSession, sub: Subscription, event: dict) -> None:
    """구독 결제(구매·갱신) payments 기록 — 매출 단일 소스. transaction_id 멱등. savepoint 감쌈."""
    tx_id = _txn_id(event.get("transaction_id"), field="transaction_id")  # 원문 저장·과대 거절
    if not tx_id:
        return
    # 사전 재조회 의미검증(§6) — payment_exists 조기반환은 다른 user/subscription이 같은
    # transaction_id를 재사용해도 멱등으로 오인해, 결제 기록 없이 증정까지 진행시켰다. 기존 결제가
    # 있으면 user_id·subscription_id 일치를 확인해 일치=멱등 no-op, 불일치=permanent(거래ID 오용).
    # (INSERT 충돌 경로와 동일 기준.)
    existing = await _payment_by_tx(session, tx_id)
    if existing is not None:
        _validated_kind(existing)  # XOR(§6) — order_id·subscription_id 중 정확히 하나(둘 다 set인 손상행 거절)
        if existing.user_id != sub.user_id or existing.subscription_id != sub.id:
            raise _permanent(
                f"결제 거래ID 재사용 tx={tx_id} 기존 user={existing.user_id} sub={existing.subscription_id}"
            )
        return  # 동일 결제 재수신 — 멱등
    amount, currency = _event_money(event)
    payment.validate_payment_target(order_id=None, subscription_id=sub.id)  # 진입점 공통 XOR
    try:
        async with session.begin_nested():
            session.add(
                Payment(
                    user_id=sub.user_id, subscription_id=sub.id, store_transaction_id=tx_id,
                    store=_store_of(event), amount=amount, currency=currency, status="paid",
                    paid_at=_ms_to_dt(event.get("purchased_at_ms")) or datetime.now(timezone.utc),
                )
            )
            await session.flush()
    except IntegrityError as exc:
        if not unique_violation(exc, _PAYMENT_TX_UQ):
            raise
        # 기존행 의미 재검증(§6) — 같은 store_transaction_id가 다른 유저·구독 결제면 오환불·위조 위험.
        existing = await _payment_by_tx(session, tx_id)
        if existing is None:
            raise  # 위반인데 조회 실패 — 예상 밖
        _validated_kind(existing)  # XOR(§6) — 손상행(둘 다 set) 거절
        if existing.user_id != sub.user_id or existing.subscription_id != sub.id:
            raise _permanent(
                f"결제 거래ID 재사용 tx={tx_id} 기존 user={existing.user_id} sub={existing.subscription_id}"
            )
        return  # 동일 결제 재수신 — 멱등


async def _grant_if_first(session: AsyncSession, uid: uuid.UUID, plan: str) -> None:
    """증정 = (user, plan) 최초 1회. savepoint로 감싸 동시 증정 UNIQUE 충돌은 멱등 처리."""
    if await _grant_exists(session, uid, plan):
        return
    try:
        async with session.begin_nested():
            tx = await hay_ledger.apply(session, uid, "subscription_grant", HAY_GRANT[plan])
            session.add(SubscriptionHayGrant(user_id=uid, plan=plan, hay_transaction_id=tx.id))
            await session.flush()
    except IntegrityError as exc:
        if not unique_violation(exc, _GRANT_UQ):
            raise
        return  # 동시 증정 — 이미 지급됨(멱등)


# ─────────────────────────────────────────────────────────────
# 환불 회수·복구(§4) — Payment.user_id 대상, 실 원장액 권위, 정확 transaction_id
# ─────────────────────────────────────────────────────────────
async def _payment_by_tx(session: AsyncSession, tx_id: str) -> Payment | None:
    return (
        await session.execute(
            select(Payment).where(Payment.store_transaction_id == tx_id).with_for_update()
        )
    ).scalars().first()


def _validated_kind(pay: Payment) -> str:
    try:
        return payment.validate_payment_target(pay.order_id, pay.subscription_id)
    except ValueError as exc:
        raise _permanent(str(exc)) from exc


async def _handle_cancellation(
    session: AsyncSession, event: dict, event_ts: datetime | None
) -> HandlerResult:
    """CANCELLATION — cancel_reason=CUSTOMER_SUPPORT(환불)만 회수 / 그 외(UNSUBSCRIBE 등)는 자동갱신 off."""
    if event.get("cancel_reason") == "CUSTOMER_SUPPORT":
        return await _handle_refund(session, event, event_ts)
    original_tx = _txn_id(  # 조회도 저장과 동일 정규화·과대(>512) 거절(SOMA-372)
        event.get("original_transaction_id") or event.get("transaction_id"),
        field="original_transaction_id",
    )
    sub = await _by_original_tx(session, original_tx, lock=True)
    if sub is None:
        return HandlerResult(DEPENDENCY_MISSING, f"자동갱신 해제 대상 구독 없음 {original_tx}")
    # 자동갱신 해제 — 만료 전까지 혜택 유지. auto_renew=false 후퇴는 단조 예외 아님(가드 적용).
    if not _state_applies(sub, event_ts):
        return HandlerResult(NO_OP, "자동갱신 해제 — 옛 상태 이벤트 skip")
    sub.auto_renew_enabled = False
    _bump_last_event(sub, event_ts)
    return HandlerResult(HANDLED, None)


async def _handle_refund(
    session: AsyncSession, event: dict, event_ts: datetime | None
) -> HandlerResult:
    """환불 회수 — 정확한 transaction_id로 Payment 잠금(original_tx 폴백 금지, 다중갱신 오환불)."""
    tx_id = _txn_id(event.get("transaction_id"), field="transaction_id")  # 조회도 저장과 동일 정규화·과대 거절
    if not tx_id:
        raise _permanent("환불 이벤트 거래 ID 없음")
    pay = await _payment_by_tx(session, tx_id)
    if pay is None:
        raise _dependency(f"환불 대상 결제 없음 tx={tx_id}")  # pending 수렴
    kind = _validated_kind(pay)
    if kind == "subscription":
        return await _refund_subscription(session, pay, event_ts)
    return await _refund_pack(session, pay)


async def _refund_subscription(
    session: AsyncSession, pay: Payment, event_ts: datetime | None
) -> HandlerResult:
    sub = await session.get(Subscription, pay.subscription_id, with_for_update=True)
    if sub is None:
        raise _permanent(f"결제의 구독 없음 sub={pay.subscription_id}")
    # 환불은 종단 — 상태 revoked(옛 RENEWAL의 큰 expires로 재활성화되지 않게 last_event_at도 전진).
    if sub.status != "revoked":
        sub.status = "revoked"
    _bump_last_event(sub, event_ts)
    # 증정 회수 — 대상 = Payment.user_id, 회수량 = 실제 원장액(grant.hay_transaction_id→amount).
    await _clawback_subscription_grant(session, pay.user_id, sub.plan)
    if pay.status != "refunded":
        pay.status = "refunded"
    return HandlerResult(HANDLED, None)


async def _clawback_subscription_grant(
    session: AsyncSession, user_id: uuid.UUID, plan: str
) -> None:
    grant = (
        await session.execute(
            select(SubscriptionHayGrant)
            .where(SubscriptionHayGrant.user_id == user_id, SubscriptionHayGrant.plan == plan)
            .with_for_update()
        )
    ).scalars().first()
    if grant is None:  # 회수 대상 증정 없음 — 성공 no-op 금지(§4·§11.2): 관측 후 수동
        raise _permanent(f"환불 회수 대상 증정 없음 user={user_id} plan={plan}")
    if grant.revoked_at is not None:
        return  # 이미 회수 — 멱등(revoked_at 표식)
    intended = await _grant_ledger_amount(session, grant)
    if intended <= 0:  # 원 증정 원장 없음/0/음수 — 성공 no-op 금지(§11.2)
        raise _permanent(f"증정 원장액 이상 grant={grant.id} amount={intended}")
    # 음수 부채 회수 — 잔액이 음수로 내려가 증정 소비분까지 완전 회수(이후 획득 자연 상계).
    tx = await hay_ledger.apply(session, user_id, "refund_revoke", -intended, allow_negative=True)
    grant.revoked_at = datetime.now(timezone.utc)
    grant.clawback_hay_transaction_id = tx.id


async def _refund_pack(session: AsyncSession, pay: Payment) -> HandlerResult:
    if pay.status == "refunded":
        return HandlerResult(NO_OP, "팩 환불 멱등(이미 refunded)")
    intended = await payment.pack_ledger_amount(session, pay.order_id)
    if intended <= 0:  # 실제 원장액 없음/0/음수 — 성공 no-op 금지(§11.2)
        raise _permanent(f"팩 환불 원장액 이상 order={pay.order_id} amount={intended}")
    await hay_ledger.apply(
        session, pay.user_id, "refund_revoke", -intended,
        order_id=pay.order_id, allow_negative=True,
    )
    pay.status = "refunded"
    ord_ = await session.get(Order, pay.order_id, with_for_update=True)
    if ord_ is not None and ord_.status != "refunded":
        ord_.status = "refunded"
    return HandlerResult(HANDLED, None)


async def _handle_refund_reversed(
    session: AsyncSession, event: dict, event_ts: datetime | None
) -> HandlerResult:
    """REFUND_REVERSED — 환불 취소(재지급). 반대부호 복구 + status/payment/order 복구. 결제 생성 안 함."""
    tx_id = _txn_id(event.get("transaction_id"), field="transaction_id")  # 조회도 저장과 동일 정규화·과대 거절
    if not tx_id:
        raise _permanent("복구 이벤트 거래 ID 없음")
    pay = await _payment_by_tx(session, tx_id)
    if pay is None:
        raise _dependency(f"복구 대상 결제 없음 tx={tx_id}")
    kind = _validated_kind(pay)
    if kind == "subscription":
        return await _reverse_subscription_refund(session, pay, event_ts)
    return await _reverse_pack_refund(session, pay)


async def _reverse_subscription_refund(
    session: AsyncSession, pay: Payment, event_ts: datetime | None
) -> HandlerResult:
    sub = await session.get(Subscription, pay.subscription_id, with_for_update=True)
    if sub is None:
        raise _permanent(f"복구 결제의 구독 없음 sub={pay.subscription_id}")
    grant = (
        await session.execute(
            select(SubscriptionHayGrant)
            .where(SubscriptionHayGrant.user_id == pay.user_id, SubscriptionHayGrant.plan == sub.plan)
            .with_for_update()
        )
    ).scalars().first()
    if grant is None:  # 복구할 증정 자체가 없음 — 실오류(관측 후 수동), 성공 no-op 금지
        raise _permanent(f"복구 대상 증정 없음 user={pay.user_id} plan={sub.plan}")
    if grant.revoked_at is None:  # 회수 미기록(환불이 아직 안 옴) — 환불 선행 대기(다음 틱 수렴)
        raise _dependency(f"복구 대상 회수 미기록(환불 선행 대기) grant={grant.id}")
    intended = await _grant_ledger_amount(session, grant)
    if intended <= 0:  # 레거시(v6 이전) 회수 원장 불명 — 정확 복원 불가. 관측 후 수동(실오류로 노출).
        raise _permanent(f"복구 원장액 이상 grant={grant.id} amount={intended}")
    await hay_ledger.apply(session, pay.user_id, "admin_adjustment", intended, allow_negative=True)
    grant.revoked_at = None
    grant.clawback_hay_transaction_id = None
    if pay.status == "refunded":
        pay.status = "paid"
    if sub.status == "revoked":
        sub.status = "active"  # 복구(만료 실효성은 expires_at·후속 갱신이 관리)
    _bump_last_event(sub, event_ts)
    return HandlerResult(HANDLED, None)


async def _reverse_pack_refund(session: AsyncSession, pay: Payment) -> HandlerResult:
    if pay.status != "refunded":
        return HandlerResult(NO_OP, "복구 대상 팩 환불 이력 없음")
    restored = await payment.restore_pack(session, pay.user_id, pay.order_id)
    if restored <= 0:
        raise _permanent(f"팩 복구 원장액 이상 order={pay.order_id}")
    pay.status = "paid"
    ord_ = await session.get(Order, pay.order_id, with_for_update=True)
    if ord_ is not None and ord_.status == "refunded":
        ord_.status = "paid"
    return HandlerResult(HANDLED, None)


async def _grant_ledger_amount(session: AsyncSession, grant: SubscriptionHayGrant) -> int:
    """증정 원장액(양수) — 회수/복구량 권위. hay_transaction_id→amount. 상수·카탈로그 재조회 금지."""
    if grant.hay_transaction_id is None:
        return 0
    tx = await session.get(HayTransaction, grant.hay_transaction_id)
    return int(tx.amount) if tx is not None else 0


# ─────────────────────────────────────────────────────────────
# 만료·유예
# ─────────────────────────────────────────────────────────────
async def _handle_expiration(
    session: AsyncSession, event: dict, event_ts: datetime | None
) -> HandlerResult:
    original_tx = _txn_id(  # 조회도 저장과 동일 정규화·과대(>512) 거절(SOMA-372)
        event.get("original_transaction_id") or event.get("transaction_id"),
        field="original_transaction_id",
    )
    sub = await _by_original_tx(session, original_tx, lock=True)
    if sub is None:
        return HandlerResult(DEPENDENCY_MISSING, f"만료 대상 구독 없음 {original_tx}")
    if sub.status == "revoked":
        return HandlerResult(NO_OP, "revoked 구독 — 만료 skip")
    expires = _ms_to_dt(event.get("expiration_at_ms"))
    # EXPIRATION 이중(§3): expires < sub.expires_at 이면 옛 만료 — 무시.
    if expires is not None and sub.expires_at is not None and expires < sub.expires_at:
        return HandlerResult(NO_OP, "옛 만료 이벤트(expires < 현재) skip")
    if not _state_applies(sub, event_ts):
        return HandlerResult(NO_OP, "옛 상태 이벤트(expiration) skip")
    sub.status = "expired"
    _bump_last_event(sub, event_ts)
    return HandlerResult(HANDLED, None)


async def _handle_billing_issue(
    session: AsyncSession, event: dict, event_ts: datetime | None
) -> HandlerResult:
    original_tx = _txn_id(  # 조회도 저장과 동일 정규화·과대(>512) 거절(SOMA-372)
        event.get("original_transaction_id") or event.get("transaction_id"),
        field="original_transaction_id",
    )
    sub = await _by_original_tx(session, original_tx, lock=True)
    if sub is None:
        return HandlerResult(DEPENDENCY_MISSING, f"유예 대상 구독 없음 {original_tx}")
    if sub.status == "revoked":
        return HandlerResult(NO_OP, "revoked 구독 — 유예 skip")
    if not _state_applies(sub, event_ts):
        return HandlerResult(NO_OP, "옛 상태 이벤트(billing_issue) skip")
    sub.status = "grace_period"  # 유예 — 혜택 유지
    _bump_last_event(sub, event_ts)
    return HandlerResult(HANDLED, None)
