"""Real model + existing dev DB; synthetic source/state changes always roll back."""
import os
import uuid
from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core import db
from app.config import settings
from app.services import checkpoint, checkpoint_repo, context_retention, usage_ledger
from db.envfile import assert_dev_target

pytest_plugins = ["tests.integration.test_checkpoint_continuity_dev"]


@pytest_asyncio.fixture(autouse=True)
async def close_ledger_pool():
    if os.getenv("MOLY_RETENTION_MODEL_TEST_ENV") != "dev":
        pytest.skip("explicit real-model development opt-in required")
    env_path = os.getenv("MOLY_CHECKPOINT_ENV_FILE", ".env")
    assert_dev_target(env_path, settings.supabase_db_connection_string)
    if db._engine is not None:
        assert_dev_target(env_path, db._engine.url.render_as_string(hide_password=False))
    yield
    await usage_ledger.flush_closes()
    if db._engine is not None:
        await db._engine.dispose()
        db._engine = None
        db._sessionmaker = None


@pytest.mark.parametrize(("language", "body", "next_body"), [
    ("ko", "이번 주말 부산 여행 준비 중이야. 숙소를 아직 못 골랐어. 바다 근처와 역 근처 중 어디가 좋을까?", "오늘 점심에 비빔밥 먹었어."),
    ("en", "I am planning a trip to Busan this weekend. I have not chosen a hotel. Should I stay near the beach or the station?", "I had bibimbap for lunch today."),
    ("ja", "今週末の釜山旅行を準備してる。まだ宿を決めてない。海の近くと駅の近く、どっちがいいかな？", "今日のお昼はビビンバを食べたよ。"),
])
async def test_real_model_delta_publication_preserves_open_questions(checkpoint_db, language, body, next_body):
    if os.getenv("MOLY_RETENTION_MODEL_TEST_ENV") != "dev":
        pytest.skip("explicit real-model development opt-in required")
    session, (uid, _) = checkpoint_db
    previous = None
    first_open = set()
    for sequence, source_body in enumerate((body, next_body), start=1):
        mid = (await session.execute(text("""
          INSERT INTO messages(user_id,sender,kind,content,activity_date,turn_seq,turn_position)
          VALUES(:u,'user','normal',:body,CURRENT_DATE,:seq,1) RETURNING id
        """), {"u": uid, "body": source_body, "seq": sequence})).scalar_one()
        await session.execute(text("""
          INSERT INTO memory_pipeline_states(user_id,mode,bootstrap_status,source_through_turn_seq,
            ingest_through_turn_seq,consolidated_through_turn_seq)
          VALUES(:u,'v2','ready',:seq,:seq,:seq)
          ON CONFLICT(user_id) DO UPDATE SET mode='v2',bootstrap_status='ready',
            source_through_turn_seq=:seq,ingest_through_turn_seq=:seq,consolidated_through_turn_seq=:seq
        """), {"u": uid, "seq": sequence})
        await session.execute(text("""UPDATE chat_contexts SET contract_generation=0,
          contract_processed_through_message_id=:mid WHERE user_id=:u"""), {"u": uid, "mid": mid})
        sources = await checkpoint_repo.load_range(session, uid,
            after_id=previous.through_message_id if previous else 0, through_id=mid, include_excluded=True)
        summary, state, _ = await checkpoint.summarize_progress(
            messages=sources, previous=previous, language=language, nickname=None, timeout=30,
            ledger=usage_ledger.LedgerContext(lane="background", purpose="context_summary"),
        )
        await context_retention.validate_progress_sources(session, user_id=uid, state=state)
        assert await context_retention.processing_complete(session, user_id=uid, generation=0, through_id=mid)
        digest = checkpoint.source_hash(previous=previous, messages=sources,
                                        version=checkpoint.RETENTION_SUMMARIZER_VERSION)
        identity = await checkpoint_repo.insert(session, user_id=uid, through_message_id=mid,
            summary=summary, source_hash=digest, expected_generation=0,
            version=checkpoint.RETENTION_SUMMARIZER_VERSION, state_json=state,
            validation_json={"processing_complete": True}, coverage_from=sources[0].id,
            previous_id=previous.id if previous else None)
        assert isinstance(identity, uuid.UUID)
        loaded = await checkpoint_repo.load_latest(session, uid, retention_enabled=True)
        assert loaded.id == identity and loaded.state_json == state
        opened = {item["id"] for item in state["items"]
                  if item["kind"] == "open_question" and item["status"] == "active"}
        if sequence == 1:
            assert opened, "the explicit unresolved travel question must be represented"
            first_open = opened
        else:
            assert first_open <= opened, "an unrelated lunch message must not remove unresolved questions"
        previous = replace(loaded, state_json=state)
    await usage_ledger.flush_closes()
