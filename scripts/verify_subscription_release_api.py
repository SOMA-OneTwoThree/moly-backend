"""Exercise the deployed dev auth/chat/fortune APIs with disposable synthetic accounts.

Only fixed dev endpoints and dev Supabase are accepted. Restores rollout settings
and deletes only accounts created by this run. Never prints tokens or credentials.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import uuid

import asyncpg
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.envfile import assert_dev_target, load_conn  # noqa: E402
from scripts.dev_token import admin_headers, config, mint_token  # noqa: E402

API = "https://dev.moly.asia"
AUTH = "https://moly-server-dev.vercel.app"
LIMITS = {"en": (40_000, 300_000), "ko": (50_000, 400_000), "ja": (60_000, 550_000)}
TEXT = {
    "en": ["I finished my presentation today. I was nervous but I'm relieved now.",
           "I practiced a lot yesterday, so I'm happy it went well. Just celebrate with me.",
           "I want to have a nice dinner and take a walk tonight.", "What did I say I finished today?"],
    "ko": ["오늘 발표를 끝냈어. 많이 긴장했는데 잘 마쳐서 마음이 편해졌어.",
           "어제 늦게까지 연습했거든. 잘 끝나서 기뻐. 조언 말고 같이 기뻐해 줘.",
           "오늘 저녁에는 맛있는 밥을 먹고 산책하고 싶어.", "내가 오늘 뭘 끝냈다고 했지?"],
    "ja": ["今日、発表が終わったよ。緊張したけど、無事に終わってほっとした。",
           "昨日遅くまで練習したんだ。うまくいって嬉しい。アドバイスより一緒に喜んでほしいな。",
           "今夜はおいしいご飯を食べて散歩したいな。", "今日、何が終わったって話したっけ？"],
}


async def run(args):
    values = {}
    for raw in Path(args.env_file).read_text().splitlines():
        if raw.strip() and not raw.lstrip().startswith("#") and "=" in raw:
            k, v = raw.split("=", 1)
            values[k.strip()] = v.strip().strip('"').strip("'")
    url, secret, public = config(values)
    assert url == "https://wywzjslvxwttxkecbyis.supabase.co"
    dsn = load_conn(args.env_file)
    assert_dev_target(args.env_file, dsn)
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=10, command_timeout=20)
    users = []
    report = {"api": API, "auth": AUTH, "checks": [], "chat_usage": {}}
    saved = {r["key"]: r["value"] for r in await conn.fetch(
        "SELECT key,value FROM public.app_config WHERE key=ANY($1::text[])",
        ["subscription_launch", "free_launch_until"])}
    written = {}

    async def setting(key, value):
        encoded = json.dumps(value)
        await conn.execute("INSERT INTO public.app_config(key,value) VALUES($1,$2::jsonb) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()", key, encoded)
        written[key] = value

    async def request(client, method, path, expected=200, **kwargs):
        response = await client.request(method, path, **kwargs)
        assert response.status_code == expected, f"{method} {path}: {response.status_code}, expected {expected}"
        return response.json() if response.content else None

    def passed(name):
        report["checks"].append(name)
        print("PASS " + name, flush=True)

    try:
        async with httpx.AsyncClient(timeout=60) as net:
            health = await request(net, "GET", API + "/health")
            assert health["version"] == args.expected_sha
            await request(net, "GET", API + "/health/ready")
            await request(net, "GET", AUTH + "/health")
            await request(net, "GET", AUTH + "/me", expected=401)
            for lang in LIMITS:
                email = f"subscription-release+{uuid.uuid4().hex}@moly.test"
                created = await request(net, "POST", url + "/auth/v1/admin/users",
                    headers=admin_headers(secret), json={"email": email, "email_confirm": True, "password": uuid.uuid4().hex})
                uid = uuid.UUID(created["id"])
                users.append({"id": uid, "lang": lang})
                with httpx.Client(timeout=30) as sync:
                    token = mint_token(sync, url, secret, public, email)["access_token"]
                user = users[-1]
                user["auth"] = httpx.AsyncClient(base_url=AUTH, timeout=60, headers={"Authorization": f"Bearer {token}"})
                user["api"] = httpx.AsyncClient(base_url=API, timeout=60, headers={"Authorization": f"Bearer {token}"})
                await request(user["auth"], "POST", "/onboarding", json={"nickname": "QuotaTest", "language": lang, "timezone": "Asia/Seoul"})
            passed("dev identities authenticated and onboarded on both services")
            await setting("subscription_launch", {"enabled": False})
            await setting("free_launch_until", "2020-01-01T00:00:00Z")
            for u in users:
                me = await request(u["auth"], "GET", "/me")
                state = await request(u["api"], "GET", "/chat/state")
                assert me["entitlement"]["daily_token_limit"] == state["daily_token_limit"] == 150_000
                assert me["entitlement"]["entitlement_source"] == "launch"
                assert me["subscription_rollout"]["enabled"] is False
                await request(u["auth"], "POST", "/subscription/trial", expected=409)
            passed("prepared release preserves launch quota despite old expiry; enrollment stays closed")
            now = datetime.now(timezone.utc)
            await setting("subscription_launch", {"enabled": True, "existing_user_cutoff": (now + timedelta(days=1)).isoformat()})
            for u in users:
                assert (await request(u["api"], "GET", "/chat/state"))["daily_token_limit"] == 150_000
                assert not (await request(u["auth"], "GET", "/me"))["subscription_rollout"]["enabled"]
            passed("future cutoff does not activate subscription early")
            await setting("subscription_launch", {"enabled": True, "existing_user_cutoff": (now - timedelta(hours=1)).isoformat()})
            for u in users:
                paid = LIMITS[u["lang"]][1]
                for _ in range(2):
                    me = await request(u["auth"], "POST", "/subscription/trial")
                    assert me["entitlement"]["daily_token_limit"] == paid
                    assert me["entitlement"]["entitlement_source"] == "signup_trial"
                duration = await conn.fetchval("SELECT app_trial_ends_at-app_trial_started_at FROM public.profiles WHERE id=$1", u["id"])
                assert duration == timedelta(hours=48)
                assert (await request(u["api"], "GET", "/chat/state"))["daily_token_limit"] == paid
                await request(u["api"], "PUT", "/daily-fortune/profile", json={"birth_date": "2002-12-13", "gender": "man"})
                fortune = await request(u["api"], "POST", "/daily-fortune/reveal")
                assert fortune["state"] == "revealed" and fortune["access"] == "included"
                assert len(fortune["result"]["categories"]) == 4
            passed("all locales: signup trial equals paid allowance, retry preserves 48h, fortune needs no ad")
            for u in users:
                deltas = []
                for index in range(args.turns):
                    before = await request(u["api"], "GET", "/chat/state")
                    key = "subscription-test-" + uuid.uuid4().hex
                    body = {"text": TEXT[u["lang"]][index % 4]}
                    response = await request(u["api"], "POST", "/chat/messages", headers={"Idempotency-Key": key}, json=body)
                    after = await request(u["api"], "GET", "/chat/state")
                    delta = after["tokens_used"] - before["tokens_used"]
                    assert delta > 0
                    deltas.append(delta)
                    if index == 0:
                        replay = await request(u["api"], "POST", "/chat/messages", headers={"Idempotency-Key": key}, json=body)
                        assert response == replay
                        assert (await request(u["api"], "GET", "/chat/state"))["tokens_used"] == after["tokens_used"]
                        await request(u["api"], "POST", "/chat/messages", expected=409,
                                      headers={"Idempotency-Key": key}, json={"text": "different"})
                    print(f"chat {u['lang']} {index + 1}/{args.turns}: {delta} units", flush=True)
                report["chat_usage"][u["lang"]] = {"turns": len(deltas), "units": deltas, "mean": round(sum(deltas) / len(deltas))}
            passed("GPT-6 conversations and memory context; identical retries never charge twice")
            for u in users:
                free, paid = LIMITS[u["lang"]]
                used = (await request(u["api"], "GET", "/chat/state"))["tokens_used"]
                await conn.execute("UPDATE public.profiles SET app_trial_ends_at=now()-interval '1 second' WHERE id=$1", u["id"])
                me = await request(u["auth"], "GET", "/me")
                state = await request(u["api"], "GET", "/chat/state")
                assert me["entitlement"]["daily_token_limit"] == state["daily_token_limit"] == free
                assert me["entitlement"]["tokens_used"] == used and not state["personal_diary_eligible"]
                await conn.execute("DELETE FROM public.daily_fortunes WHERE user_id=$1", u["id"])
                fortune = await request(u["api"], "POST", "/daily-fortune/reveal")
                assert fortune["state"] == "locked" and fortune["access"] == "ad_required"
                assert "categories" not in fortune["result"]
                await conn.execute("UPDATE public.user_daily_stats SET tokens_used=$2 WHERE user_id=$1 AND activity_date=$3",
                                   u["id"], free, datetime.fromisoformat(state["activity_date"]).date())
                await request(u["api"], "POST", "/chat/messages", expected=403,
                              headers={"Idempotency-Key": "limit-" + uuid.uuid4().hex}, json={"text": "hello"})
                for status in ("active", "grace_period", "expired", "revoked"):
                    await conn.execute("""INSERT INTO public.subscriptions(user_id,plan,status,original_transaction_id,purchased_at,expires_at,environment)
                        VALUES($1,'monthly',$2,$3,now(),now()+interval '30 days','SANDBOX')
                        ON CONFLICT(original_transaction_id) DO UPDATE SET status=excluded.status""",
                        u["id"], status, "release-test-" + str(u["id"]))
                    entitled = status in {"active", "grace_period"}
                    me = await request(u["auth"], "GET", "/me")
                    state = await request(u["api"], "GET", "/chat/state")
                    assert me["entitlement"]["daily_token_limit"] == state["daily_token_limit"] == (paid if entitled else free)
                    assert me["entitlement"]["tokens_used"] == free
                    if entitled:
                        fortune = await request(u["api"], "POST", "/daily-fortune/reveal")
                        assert fortune["state"] == "revealed" and "categories" in fortune["result"]
                passed(f"{u['lang']}: free limit blocks chat; paid/grace/expiry/refund retain counters and correct benefits")
            # Real model and pricing evidence, after the async ledger flusher completes.
            await asyncio.sleep(3)
            rows = await conn.fetch("""SELECT model,status,count(*) AS calls FROM public.ai_usage_ledger
                WHERE user_id=ANY($1::uuid[]) AND lane='foreground' GROUP BY model,status""", [u["id"] for u in users])
            report["model_calls"] = [dict(r) for r in rows]
            assert rows and all(r["model"] == "gpt-6-luna" and r["status"] == "completed" for r in rows)
            missing_prices = await conn.fetchval("""SELECT count(*) FROM public.ai_usage_ledger
                WHERE user_id=ANY($1::uuid[]) AND lane='foreground'
                AND (price_catalog_version IS DISTINCT FROM 20260923 OR cost_micro_usd IS NULL)""", [u["id"] for u in users])
            assert missing_prices == 0
            passed("deployed chat ledger confirms GPT-6 Luna and completed usage records")
    finally:
        for key, value in written.items():
            actual = await conn.fetchval("SELECT value FROM public.app_config WHERE key=$1", key)
            if actual is None or json.loads(actual) != value:
                raise RuntimeError("Concurrent dev config edit: refusing to overwrite " + key)
            if key in saved:
                await conn.execute("UPDATE public.app_config SET value=$2::jsonb,updated_at=now() WHERE key=$1", key, saved[key])
            else:
                await conn.execute("DELETE FROM public.app_config WHERE key=$1", key)
        async with httpx.AsyncClient(timeout=30) as cleanup:
            for u in users:
                if "auth" in u:
                    response = await u["auth"].delete("/me")
                    assert response.status_code == 204, "Synthetic account cleanup failed"
                    await u["auth"].aclose()
                    await u["api"].aclose()
                else:
                    await cleanup.delete(url + "/auth/v1/admin/users/" + str(u["id"]), headers=admin_headers(secret))
        await conn.close()
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print("Dev rollout settings restored; disposable accounts removed", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--turns", type=int, choices=range(4, 13), default=8)
    parser.add_argument("--output", required=True)
    asyncio.run(run(parser.parse_args()))
