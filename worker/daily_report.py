"""Daily service totals and the existing operations message."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.core.db import get_sessionmaker
from app.models.app_config import AppConfig

KST = ZoneInfo("Asia/Seoul")
TOTALS_KEY = "monitoring:daily_totals"
SUBSCRIPTIONS_KEY = "monitoring:subscription_totals"
COUNT_KEYS = (
    "diaries", "diary_llm", "diary_preset", "diary_none", "diary_failed", "timed_out",
    "deadline_skipped", "push_sent", "push_invalid_token", "push_transient", "push_payload_error",
    "push_setup_error", "rc_exception", "rc_processor_error",
)


def window(now: datetime) -> tuple[datetime, datetime]:
    end = datetime.combine(now.astimezone(KST).date(), time(), KST).astimezone(timezone.utc)
    return end - timedelta(days=1), end


def add_totals(previous: dict | None, now: datetime, counts: dict) -> dict:
    result = dict(previous or {})
    last = result.get("last_tick")
    if last and datetime.fromisoformat(last) >= now:
        return result
    result.setdefault("started_at", now.isoformat())
    day = now.astimezone(KST).date()
    days = {key: dict(value) for key, value in result.get("days", {}).items()
            if key >= (day - timedelta(days=3)).isoformat()}
    bucket = dict(days.get(day.isoformat(), {}))
    bucket["runs"] = bucket.get("runs", 0) + 1
    for key in COUNT_KEYS:
        value = counts.get(key, 0)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            bucket[key] = bucket.get(key, 0) + value
    days[day.isoformat()] = bucket
    return {**result, "last_tick": now.isoformat(), "days": days}


async def record_tick(now: datetime, counts: dict) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(text("SET LOCAL lock_timeout='1s'"))
        await session.execute(text("SET LOCAL statement_timeout='2500ms'"))
        await session.execute(insert(AppConfig).values(key=TOTALS_KEY, value={})
                              .on_conflict_do_nothing())
        row = (await session.execute(select(AppConfig).where(AppConfig.key == TOTALS_KEY)
                                     .with_for_update())).scalar_one()
        row.value = add_totals(row.value, now, counts)
        row.updated_at = now
        await session.commit()


_ACTIVITY_SQL = text("""
WITH activity AS (
 SELECT user_id,created_at seen_at FROM messages
 WHERE sender='user' AND kind='normal' AND created_at>=:month_start AND created_at<:end
 UNION ALL SELECT user_id,created_at FROM greetings
 WHERE context='home_enter' AND created_at>=:month_start AND created_at<:end
 UNION ALL SELECT user_id,created_at FROM mood_entries
 WHERE created_at>=:month_start AND created_at<:end
 UNION ALL SELECT user_id,completed_at FROM routine_completions
 WHERE completed_at>=:month_start AND completed_at<:end
 UNION ALL SELECT user_id,created_at FROM fortune_ad_sessions
 WHERE created_at>=:month_start AND created_at<:end
 UNION ALL SELECT user_id,created_at FROM training_submissions
 WHERE created_at>=:month_start AND created_at<:end
 UNION ALL SELECT user_id,read_at FROM article_progress
 WHERE read_at>=:month_start AND read_at<:end
 UNION ALL SELECT user_id,first_read_at FROM diaries
 WHERE first_read_at>=:month_start AND first_read_at<:end
 UNION ALL SELECT user_id,created_at FROM hay_transactions
 WHERE type IN ('attendance','ad_reward','shop_purchase')
 AND created_at>=:month_start AND created_at<:end
)
SELECT count(DISTINCT user_id) FILTER(WHERE seen_at>=:start) dau,
 count(DISTINCT user_id) FILTER(WHERE seen_at>=:prior_start AND seen_at<:start) prior_dau,
 count(DISTINCT user_id) mau,
 (SELECT count(*) FROM profiles WHERE created_at>=:start AND created_at<:end) signups,
 (SELECT count(*) FROM profiles WHERE created_at>=:prior_start AND created_at<:start) prior_signups
FROM activity
""")

_SUBSCRIPTIONS_SQL = text("""
WITH current_users AS (
 SELECT user_id,bool_or(store_trial_ends_at IS NULL OR store_trial_ends_at<=:now) paid
 FROM subscriptions WHERE environment='PRODUCTION' AND user_id IS NOT NULL
 AND status IN ('active','grace_period') AND expires_at>:now GROUP BY user_id
), events AS (
 SELECT payload->>'app_user_id' uid,payload->>'type' kind,
 payload->>'transaction_id' transaction_id,
 payload->>'is_trial_conversion' trial_conversion,
 CASE WHEN payload->>'purchased_at_ms'~'^[0-9]+$'
 THEN to_timestamp((payload->>'purchased_at_ms')::numeric/1000) END purchase_time,
 CASE WHEN payload->>'expiration_at_ms'~'^[0-9]+$'
 THEN to_timestamp((payload->>'expiration_at_ms')::numeric/1000) END expiration_time
 FROM revenuecat_events WHERE status='processed' AND payload->>'environment'='PRODUCTION'
), periods AS (
 SELECT uid,purchase_time,expiration_time FROM events WHERE kind IN ('INITIAL_PURCHASE','RENEWAL')
)
SELECT (SELECT count(*) FROM current_users) total,
 (SELECT count(*) FROM current_users WHERE paid) paid,
 (SELECT count(*) FROM current_users WHERE NOT paid) trial,
 count(DISTINCT uid) FILTER(WHERE kind='INITIAL_PURCHASE' AND purchase_time>=:start AND purchase_time<:end) started,
 count(DISTINCT uid) FILTER(WHERE kind='RENEWAL' AND trial_conversion='true' AND purchase_time>=:start AND purchase_time<:end
 AND EXISTS(SELECT 1 FROM payments p WHERE p.store_transaction_id=events.transaction_id
            AND p.user_id::text=events.uid AND p.subscription_id IS NOT NULL AND p.amount>0)) converted,
 count(DISTINCT uid) FILTER(WHERE kind='EXPIRATION' AND expiration_time>=:start AND expiration_time<:end
 AND NOT EXISTS(SELECT 1 FROM periods p WHERE p.uid=events.uid AND p.purchase_time<=events.expiration_time
                AND p.expiration_time>events.expiration_time)) ended
FROM events
""")

_DIARIES_SQL = text("""
WITH conversations AS (
 SELECT user_id,activity_date,sum(char_length(content)) chars FROM messages
 WHERE sender='user' AND kind='normal' AND activity_date BETWEEN :floor AND :ceiling
 GROUP BY 1,2 HAVING sum(char_length(content))>=:gate
), dates AS (
 SELECT m.*,p.timezone,p.created_at,p.app_trial_started_at,p.app_trial_ends_at,p.trial_ends_at,
 ((m.activity_date+1)+time '04:00') AT TIME ZONE p.timezone-interval '1 microsecond' boundary,
 ((m.activity_date+1)+time '09:00') AT TIME ZONE p.timezone publication
 FROM conversations m JOIN profiles p ON p.id=m.user_id
 JOIN pg_timezone_names tz ON tz.name=p.timezone
 WHERE NOT EXISTS(SELECT 1 FROM privacy_subject_barriers b WHERE b.user_id=m.user_id AND b.state<>'active')
), eligible AS (
 SELECT x.* FROM dates x WHERE publication>=:start AND publication<:end AND (
 x.boundary<:launch_until OR
 ((x.created_at AT TIME ZONE x.timezone)-interval '4 hours')::date=x.activity_date OR
 CASE WHEN x.app_trial_started_at IS NOT NULL THEN x.app_trial_ends_at
 WHEN :launch_until IS NULL OR x.trial_ends_at-interval '48 hours'>=:launch_until THEN x.trial_ends_at END>x.boundary OR
 EXISTS(SELECT 1 FROM subscriptions s WHERE s.user_id=x.user_id
 AND s.status IN ('active','grace_period','expired') AND s.expires_at>x.boundary
 AND (s.ownership_changed_at IS NULL OR s.ownership_changed_at<=x.boundary)
 AND (s.purchased_at<=x.boundary OR EXISTS(
 SELECT 1 FROM revenuecat_events e WHERE e.status='processed'
 AND e.payload->>'type' IN ('INITIAL_PURCHASE','RENEWAL')
 AND e.payload->>'original_transaction_id'=s.original_transaction_id
 AND e.payload->>'environment'=s.environment
 AND (s.ownership_changed_at IS NOT NULL OR e.payload->>'app_user_id'=s.user_id::text)
 AND CASE WHEN e.payload->>'purchased_at_ms'~'^[0-9]+$' THEN to_timestamp((e.payload->>'purchased_at_ms')::numeric/1000) END<=x.boundary
 AND (CASE WHEN e.payload->>'expiration_at_ms'~'^[0-9]+$' THEN to_timestamp((e.payload->>'expiration_at_ms')::numeric/1000) END>x.boundary
 OR (e.payload->>'transaction_id'=s.latest_transaction_id
 AND CASE WHEN e.payload->>'expiration_at_ms'~'^[0-9]+$' THEN to_timestamp((e.payload->>'expiration_at_ms')::numeric/1000) END<=s.expires_at))))))
)
SELECT count(*) expected,
 count(*) FILTER(WHERE EXISTS(SELECT 1 FROM diaries d WHERE d.user_id=x.user_id
 AND d.activity_date=x.activity_date AND d.kind='shared_day' AND d.record_status='published'
 AND d.deleted_at IS NULL AND d.published_at<=:now)) completed,
 count(*) FILTER(WHERE EXISTS(SELECT 1 FROM diaries d WHERE d.user_id=x.user_id
 AND d.activity_date=x.activity_date AND d.deleted_at IS NOT NULL AND d.kind IN ('shared_day','capi_day'))) deleted
FROM eligible x
""")


async def read_row(query, params: dict) -> dict:
    async with get_sessionmaker()() as session:
        await session.execute(text("SET LOCAL statement_timeout='2500ms'"))
        return dict((await session.execute(query, params)).mappings().one())


async def business(now: datetime, config: dict) -> dict:
    start, end = window(now)
    params = dict(start=start, end=end, now=now, prior_start=start-timedelta(days=1),
                  month_start=end-timedelta(days=30), floor=start.date()-timedelta(days=2),
                  ceiling=end.date()+timedelta(days=1), gate=config["diary_min_user_chars"],
                  launch_until=config["launch_until"])
    result = {}
    for name, query in (("activity", _ACTIVITY_SQL), ("subscriptions", _SUBSCRIPTIONS_SQL),
                        ("diaries", _DIARIES_SQL)):
        try:
            result[name] = await read_row(query, params)
        except Exception:
            result[name] = None
    return result


async def subscription_change(now: datetime, current: dict) -> int | None:
    day = now.astimezone(KST).date()
    async with get_sessionmaker()() as session:
        await session.execute(text("SET LOCAL lock_timeout='1s'"))
        await session.execute(text("SET LOCAL statement_timeout='2500ms'"))
        await session.execute(insert(AppConfig).values(key=SUBSCRIPTIONS_KEY, value={})
                              .on_conflict_do_nothing())
        row = (await session.execute(select(AppConfig).where(AppConfig.key == SUBSCRIPTIONS_KEY)
                                     .with_for_update())).scalar_one()
        previous = dict(row.value or {})
        prior = previous.get((day-timedelta(days=1)).isoformat())
        values = {key: value for key, value in previous.items()
                  if (day-timedelta(days=2)).isoformat() <= key <= day.isoformat()}
        values[day.isoformat()] = {"total": current["total"], "at": now.isoformat()}
        row.value = values
        row.updated_at = now
        await session.commit()
    return current["total"]-prior["total"] if prior else None


def delta(value: int) -> str:
    return f"+{value:,}" if value > 0 else f"{value:,}"


def render(now: datetime, data: dict, live: dict, totals: dict | None,
           cost: dict | None, change: int | None) -> str:
    start, _ = window(now)
    day = start.astimezone(KST).date().isoformat()
    notes = list(live["notes"])
    lines = [f"[운영] {now.astimezone(KST):%m/%d} 일일 보고",
             "🟢 서비스 상태 정상" if not live["notes"] else "🟡 서비스 상태 확인 필요", "", "📊 이용 현황"]
    activity = data.get("activity")
    if activity is None:
        lines.append("이용 현황: 집계 확인 필요")
    else:
        lines.extend([f"어제 신규 가입: {activity['signups']:,}명 (전날보다 {delta(activity['signups']-activity['prior_signups'])}명)",
                      f"DAU: {activity['dau']:,}명 (전날보다 {delta(activity['dau']-activity['prior_dau'])}명)",
                      f"MAU: {activity['mau']:,}명"])
    lines.extend(["", "💳 구독 현황"])
    sub = data.get("subscriptions")
    if sub is None:
        lines.append("구독 현황: 집계 확인 필요")
    else:
        difference = "전일 비교 집계 중" if change is None else f"전일 보고보다 {delta(change)}명"
        lines.extend([f"현재 구독 이용자: {sub['total']:,}명 ({difference})",
                      f" └ 유료 {sub['paid']:,}명 / 무료체험 {sub['trial']:,}명",
                      f"어제 구독 시작: {sub['started']:,}명",
                      f"어제 체험 → 유료 전환: {sub['converted']:,}명",
                      f"어제 이용 기간 종료: {sub['ended']:,}명"])
    lines.extend(["", "⚙️ 정기 작업", live["worker"], live["retention"]])
    diary = data.get("diaries")
    if diary is None:
        lines.append("개인 일기: 대상·완료 집계 확인 필요")
        notes.append("개인 일기 대상·완료 수 확인 필요")
    else:
        missing = max(0, diary["expected"]-diary["completed"]-diary["deleted"])
        lines.append(f"개인 일기: 대상 {diary['expected']:,}건 / 완료 {diary['completed']:,}건 / 누락 {missing:,}건"
                     + (f" / 삭제 제외 {diary['deleted']:,}건" if diary["deleted"] else ""))
        if missing:
            notes.append(f"개인 일기 {missing:,}건 확인 필요")
    bucket = (totals or {}).get("days", {}).get(day)
    if bucket is None:
        lines.append("푸시 알림: 일일 집계 중")
    else:
        complete = datetime.fromisoformat(totals["started_at"]) <= start
        suffix = "" if complete else " (집계 적용 이후)"
        lines.append(f"어제 정기 실행: {bucket.get('runs', 0):,}회{suffix}")
        failed = sum(bucket.get(k, 0) for k in ("push_invalid_token", "push_transient", "push_payload_error"))
        lines.append(f"푸시 알림: 발송 수락 {bucket.get('push_sent', 0):,}건 / 전송 실패 {failed:,}건{suffix}")
        setup = bucket.get("push_setup_error", 0)
        if setup:
            notes.append(f"어제 알림 발송 준비 실패 {setup:,}건")
    if any(value is None for value in data.values()):
        notes.append("일부 지표 집계 확인 필요")
    if notes:
        lines[1] = "🟡 확인할 사항 있음"
    lines.extend(["", "⚠️ 확인할 사항", *(f"• {note}" for note in dict.fromkeys(notes))] if notes
                 else ["", "확인할 사항 없음"])
    if live.get("info"):
        lines.extend("참고: "+item for item in live["info"])
    if cost is None:
        lines.extend(["", "어제 AI 사용 비용: 집계 확인 필요"])
    elif cost.get("priced"):
        lines.extend(["", f"어제 AI 사용 비용: 약 ${cost['cost']/1_000_000:.4f}"
                      + (f" ({cost['unknown']:,}건 비용 확인 중)" if cost["unknown"] else "")
                      + (f" · 실험 {cost['experiments']:,}건 포함" if cost.get("experiments") else "")])
    else:
        lines.extend(["", "어제 AI 사용 비용: 확인 중" if cost.get("calls") else "어제 AI 사용 비용: 계측 기록 없음"])
    lines.extend([f"기준: 이용·변동 {day} 한국시간 / 현재 상태 {now.astimezone(KST):%H:%M}",
                  "DAU·MAU는 기능 사용 기록 기준(앱 실행 전체와 다름), MAU는 어제까지 30일",
                  "푸시는 전송 접수 기준(기기 수신은 별도) · 비용은 사용량 기준 예상 금액"])
    return "\n".join(lines)
