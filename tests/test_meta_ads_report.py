from decimal import Decimal
import json
import logging

import httpx
from pydantic import SecretStr
import pytest

from worker import meta_ads
from tests.test_daily_report import NOW

DAY = "2026-10-19"
ACCOUNT = {"id": "act_123", "currency": "KRW", "timezone_name": "Asia/Seoul"}


def payload(**changes):
    row = dict(date_start=DAY, date_stop=DAY, account_currency="KRW", spend="10001",
               actions=[{"action_type": "mobile_app_install", "value": "4"},
                        {"action_type": "omni_app_install", "value": "4"}])
    return {"data": [{**row, **changes}]}


def test_cpi_uses_only_one_install_metric_and_preserves_money():
    result = meta_ads.parse(ACCOUNT, payload(), "123", DAY)
    assert result["installs"] == 4 and result["cpi"] == Decimal("2500.25")
    lines = "\n".join(meta_ads.render(result))
    assert "광고비: ₩10,001" in lines and "CPI: ₩2,500" in lines
    assert "Meta 계정 귀속 설정" in lines


@pytest.mark.parametrize("data", [{"data": []}, payload(actions=[]), payload(actions=None)])
def test_zero_installs_are_not_a_zero_cpi(data):
    if data.get("data") and data["data"][0].get("actions") is None:
        with pytest.raises(ValueError):
            meta_ads.parse(ACCOUNT, data, "123", DAY)
        return
    result = meta_ads.parse(ACCOUNT, data, "123", DAY)
    assert result["installs"] == 0 and result["cpi"] is None
    assert "계산 불가 (설치 0건)" in "\n".join(meta_ads.render(result))


@pytest.mark.parametrize("data", [payload(spend="NaN"), payload(spend="-1"),
    payload(account_currency="USD"), payload(date_start="2026-10-18"),
    payload(actions=[{"action_type": "mobile_app_install", "value": "1.5"}]),
    payload(actions=[{"action_type": "mobile_app_install", "value": "2"}]*2),
    {**payload(), "paging": {"next": "https://untrusted.invalid/token"}},
    {"error": {"message": "private-provider-body"}},
])
def test_invalid_or_partial_responses_are_not_reported_as_success(data):
    with pytest.raises(ValueError):
        meta_ads.parse(ACCOUNT, data, "123", DAY)


@pytest.mark.parametrize("zone", ["America/Los_Angeles", "Asia/Seoul"])
def test_account_timezone_is_explicit(zone):
    result = meta_ads.parse({**ACCOUNT, "timezone_name": zone}, payload(), "123", DAY)
    assert zone in "\n".join(meta_ads.render(result))


async def test_real_request_contract_is_read_only_and_token_is_not_in_url(monkeypatch):
    monkeypatch.setattr(meta_ads.settings, "meta_ads_account_id", "act_123")
    monkeypatch.setattr(meta_ads.settings, "meta_ads_access_token", SecretStr("private-token"))
    requests = []
    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer private-token"
        assert "private-token" not in str(request.url)
        if request.url.path.endswith("/insights"):
            assert json.loads(request.url.params["time_range"]) == {"since": DAY, "until": DAY}
            assert request.url.params["level"] == "account"
            assert request.url.params["action_report_time"] == "impression"
            assert request.url.params["use_account_attribution_setting"] == "true"
            return httpx.Response(200, json=payload())
        return httpx.Response(200, json=ACCOUNT)
    factory = httpx.AsyncClient
    monkeypatch.setattr(meta_ads.httpx, "AsyncClient", lambda **kw: factory(
        **kw, transport=httpx.MockTransport(respond)))
    result = await meta_ads.snapshot(NOW)
    assert result["status"] == "ok" and len(requests) == 2


async def test_provider_failure_is_safe_and_not_a_zero(monkeypatch, caplog):
    monkeypatch.setattr(meta_ads.settings, "meta_ads_account_id", "123")
    monkeypatch.setattr(meta_ads.settings, "meta_ads_access_token", SecretStr("private-token"))
    def respond(request):
        logging.getLogger("httpcore.http11").debug("private-token private-provider-body")
        return httpx.Response(401, json={"error": "private-provider-body"})
    factory = httpx.AsyncClient
    monkeypatch.setattr(meta_ads.httpx, "AsyncClient", lambda **kw: factory(
        **kw, transport=httpx.MockTransport(respond)))
    caplog.set_level(logging.DEBUG)
    result = await meta_ads.snapshot(NOW)
    assert result == {"status": "unavailable"}
    assert "private-token" not in caplog.text and "private-provider-body" not in caplog.text
    assert "집계 확인 필요" in "\n".join(meta_ads.render(result))
    assert "₩0" not in "\n".join(meta_ads.render(result))


async def test_unconfigured_integration_does_not_call_provider(monkeypatch):
    monkeypatch.setattr(meta_ads.settings, "meta_ads_account_id", "")
    monkeypatch.setattr(meta_ads.settings, "meta_ads_access_token", SecretStr(""))
    def forbidden(**kw):
        raise AssertionError("provider called without configuration")
    monkeypatch.setattr(meta_ads.httpx, "AsyncClient", forbidden)
    result = await meta_ads.snapshot(NOW)
    assert result["status"] == "not_configured"
    assert "연결 설정 필요" in "\n".join(meta_ads.render(result))
