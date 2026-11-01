"""One account, one day: read-only Meta spend and attributed app installs."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from time import monotonic
from zoneinfo import ZoneInfo

import httpx

from app.config import settings
from app.services.slack_notify import private_transport
from worker import daily_report


def number(value) -> Decimal:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("invalid metric")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid metric") from None
    if not result.is_finite() or result < 0 or result > Decimal("1e18"):
        raise ValueError("invalid metric")
    return result


def parse(account: dict, payload: dict, account_id: str, day: str) -> dict:
    if account.get("id") != "act_"+account_id:
        raise ValueError("account mismatch")
    currency, zone = account.get("currency"), account.get("timezone_name")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("invalid currency")
    if not isinstance(zone, str):
        raise ValueError("invalid timezone")
    ZoneInfo(zone)
    rows = payload.get("data")
    if not isinstance(rows, list) or len(rows) > 1 or payload.get("paging", {}).get("next"):
        raise ValueError("incomplete account report")
    spend, installs = Decimal(0), 0
    if rows:
        row = rows[0]
        if (row.get("date_start") != day or row.get("date_stop") != day
                or row.get("account_currency") != currency):
            raise ValueError("report scope mismatch")
        spend = number(row.get("spend"))
        actions = row.get("actions", [])
        if not isinstance(actions, list) or any(not isinstance(a, dict) for a in actions):
            raise ValueError("invalid actions")
        # Do not add omni/SKAN/other overlapping install action types.
        values = [a.get("value") for a in actions if a.get("action_type") == "mobile_app_install"]
        if len(values) > 1:
            raise ValueError("duplicate install metric")
        if values:
            count = number(values[0])
            if count != count.to_integral_value():
                raise ValueError("non-integral installs")
            installs = int(count)
    return dict(status="ok", day=day, timezone=zone, currency=currency, spend=spend,
                installs=installs, cpi=spend/installs if installs else None)


async def snapshot(now: datetime) -> dict:
    account_id = settings.meta_ads_account_id.removeprefix("act_")
    token = settings.meta_ads_access_token.get_secret_value()
    if not account_id and not token:
        return {"status": "not_configured"}
    started = monotonic()
    try:
        version = settings.meta_ads_api_version
        if not re.fullmatch(r"[0-9]+", account_id) or not token or not re.fullmatch(r"v[0-9]+\.0", version):
            raise ValueError("invalid configuration")
        day = daily_report.window(now)[0].astimezone(daily_report.KST).date().isoformat()
        base = f"https://graph.facebook.com/{version}/act_{account_id}"
        # One bounded request pair per daily report, no new worker/table/cache.
        with private_transport():
            async with asyncio.timeout(2.5), httpx.AsyncClient(
                timeout=httpx.Timeout(2, connect=1), follow_redirects=False,
                headers={"Authorization": "Bearer "+token},
            ) as client:
                account = await client.get(base, params={"fields": "id,currency,timezone_name"})
                account.raise_for_status()
                payload = await client.get(base+"/insights", params={
                    "level": "account", "fields": "spend,account_currency,actions,date_start,date_stop",
                    "time_range": json.dumps({"since": day, "until": day}),
                    "action_report_time": "impression", "use_account_attribution_setting": "true",
                    "limit": "2",
                })
                payload.raise_for_status()
                return parse(account.json(), payload.json(), account_id, day)
    except Exception as exc:
        daily_report.log_failure("meta_ads", now, exc, started)
        return {"status": "unavailable"}


def money(value: Decimal, currency: str) -> str:
    places = 0 if currency in ("KRW", "JPY") else 2
    amount = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    symbol = {"KRW": "₩", "USD": "$", "JPY": "¥"}.get(currency, currency+" ")
    return f"{symbol}{amount:,.{places}f}"


def render(data: dict) -> list[str]:
    title = "📣 어제 Meta 광고 집행"
    if data["status"] != "ok":
        state = "연결 설정 필요" if data["status"] == "not_configured" else "집계 확인 필요"
        return [title, "광고비·광고 설치·CPI: "+state]
    currency = data["currency"]
    return [title, "광고비: "+money(data["spend"], currency),
            f"광고 설치: {data['installs']:,}건",
            "CPI: "+(money(data["cpi"], currency) if data["cpi"] is not None else "계산 불가 (설치 0건)"),
            f"기준: {data['day']} / {data['timezone']} / Meta 계정 귀속 설정 · 추후 변동 가능"]
