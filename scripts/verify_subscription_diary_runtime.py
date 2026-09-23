"""Run inside the deployed dev container: real Sol diaries and free-plan fallback.

Creates only disposable synthetic subjects, does not change global configuration,
and removes its subjects afterwards. Refuses local and production environments.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
import json
from pathlib import Path
import sys
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings  # noqa: E402
from app.core.db import get_engine, get_sessionmaker  # noqa: E402
from app.models.profile import Profile  # noqa: E402
from app.services import diary_generation, usage_ledger  # noqa: E402
from app.services.limits import effective_token_config  # noqa: E402

TEXT = {
    "ko": "오늘 준비하던 발표를 무사히 끝냈어. 어젯밤까지 연습해서 조금 졸리지만 마음은 편해. 동료가 잘했다고 말해줘서 기뻤어. 오늘은 집에서 따뜻한 밥을 먹고 일찍 쉬고 싶어.",
    "ja": "今日は準備していた発表が無事に終わったよ。昨日遅くまで練習したから少し眠いけど、今はほっとしてる。同僚に良かったと言ってもらえて嬉しかった。今日は家で温かいご飯を食べて早めに休みたいな。",
    "en": "I finished the presentation I had been preparing. I practiced late last night, so I'm sleepy but relieved. A colleague said I did well and that made me happy. Tonight I want a warm dinner at home and an early night.",
}


async def main():
    if (settings.environment != "development" or not Path("/.dockerenv").exists()
        or settings.supabase_url.rstrip("/") != "https://wywzjslvxwttxkecbyis.supabase.co"
        or "wywzjslvxwttxkecbyis" not in settings.supabase_db_connection_string):
        raise RuntimeError("This check is restricted to the deployed dev container and DB")
    assert settings.model_diary == "gpt-6-sol" and settings.model_utility == "gpt-6-luna"
    target = datetime.now(ZoneInfo("Asia/Seoul")).date() - timedelta(days=1)
    created = datetime.combine(target, time(12), ZoneInfo("Asia/Seoul")).astimezone(timezone.utc)
    ids = []
    results = []
    try:
        for language in TEXT:
            for eligible in (True, False):
                uid = uuid.uuid4()
                ids.append(uid)
                async with get_sessionmaker()() as session:
                    await session.execute(text("INSERT INTO auth.users(id,email,created_at) VALUES(:id,:email,:created)"),
                        {"id": uid, "email": f"diary-release+{uid.hex}@moly.test", "created": created})
                    await session.execute(text("UPDATE public.profiles SET nickname='QuotaTest',language=:language,"
                        "trial_ends_at=:trial WHERE id=:id"), {"id": uid, "language": language,
                        "trial": created + timedelta(hours=48) if eligible else created - timedelta(days=5)})
                    await session.execute(text("INSERT INTO public.messages(user_id,sender,kind,content,activity_date,created_at) "
                        "VALUES(:id,'user','normal',:content,:day,:created)"),
                        {"id": uid, "content": TEXT[language], "day": target, "created": created})
                    await session.commit()
                    profile = await session.get(Profile, uid)
                    cfg = await effective_token_config(session)
                    cfg["subscription_launch"] = {"enabled": True, "existing_user_cutoff": (created - timedelta(hours=1)).isoformat()}
                    policy = await diary_generation.load_policy(session)
                    first = await diary_generation.generate_for_user(session, profile, target, cfg, policy=policy)
                    assert first["personal_attempted"] is eligible, first
                    if eligible:
                        assert first["created"] and not first["empty_body"], first
                        row = (await session.execute(text("SELECT source,content FROM public.diaries WHERE user_id=:id AND activity_date=:day"),
                            {"id": uid, "day": target})).mappings().one()
                        assert row["source"] == "llm" and len(row["content"].strip()) >= 40
                    profile = await session.get(Profile, uid, populate_existing=True)
                    second = await diary_generation.generate_for_user(session, profile, target, cfg, policy=policy)
                    assert second["skipped"] and second["reason"] == "already_exists"
                    await usage_ledger.flush_closes()
                    ledger = (await session.execute(text("SELECT purpose,model,status,price_catalog_version,cost_micro_usd "
                        "FROM public.ai_usage_ledger WHERE user_id=:id"), {"id": uid})).mappings().all()
                    generation = [r for r in ledger if r["purpose"] == "diary_generate"]
                    assert bool(generation) is eligible
                    if eligible:
                        assert all(r["model"] == "gpt-6-sol" and r["status"] == "completed" for r in generation)
                        assert all(r["price_catalog_version"] == 20260923 and r["cost_micro_usd"] is not None for r in generation)
                        checks = [r for r in ledger if r["purpose"] == "diary_self_check"]
                        assert checks and all(r["model"] == "gpt-6-luna" and r["status"] == "completed" for r in checks)
                    results.append({"language": language, "personal_eligible": eligible,
                                    "created": first["created"], "idempotent": True, "calls": len(ledger)})
                    print(json.dumps(results[-1]), flush=True)
    finally:
        async with get_sessionmaker()() as session:
            for uid in ids:
                await session.execute(text("DELETE FROM auth.users WHERE id=:id AND email=:email"),
                    {"id": uid, "email": f"diary-release+{uid.hex}@moly.test"})
            await session.commit()
        await get_engine().dispose()
        print("Disposable diary subjects removed; global configuration unchanged", flush=True)
    print(json.dumps({"status": "passed", "results": results}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
