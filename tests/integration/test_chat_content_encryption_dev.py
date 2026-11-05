"""Existing dev Supabase; synthetic sources and policy edits are rolled back."""
import base64
import hashlib
import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core import content_crypto
from app.models.message import Message
from app.models.greeting import Greeting
from app.models.idempotency_key import IdempotencyKey
from app.services import chat, checkpoint, checkpoint_repo, context_retention
from app.services import message_content as storage
from app.services.chat_content_encryption import EncryptionMode, load_mode
from app.services.chat_content_encryption import activate
from app.services.chat_turns import verify_publish as verify_actual, Lease
from app.services.context_provenance import SourceRef
from tests.integration.test_checkpoint_continuity_dev import checkpoint_db as existing_dev_fixture

checkpoint_db = existing_dev_fixture


@pytest.fixture(autouse=True)
def synthetic_keys(monkeypatch):
    ring = content_crypto.parse_keyring(json.dumps({"environment": "development", "keys": {
        "1": base64.b64encode(b"s" * 32).decode()}}), environment="development")
    monkeypatch.setattr(content_crypto, "_cached_keyring", ring)


@pytest.mark.parametrize("kind", ["normal", "safety", "safety_check", "greeting", "topic_opening", "fortune_derived"])
async def test_old_and_new_source_read_hash_and_no_orm_rewrite(checkpoint_db, kind):
    session, (uid, _) = checkpoint_db
    texts = ["기존 한글🙂 日本語", "새로 저장한 {유저이름}의 대화🙂"]
    rows = []
    stamp = datetime.now(timezone.utc)
    for index, value in enumerate(texts):
        row = Message(user_id=uid, sender="user", kind=kind, activity_date=date.today(), created_at=stamp,
                      **storage.encode_message(user_id=uid, text=value,
                          mode=EncryptionMode(bool(index), 1, 1, (1,))))
        session.add(row)
        rows.append(row)
    await session.flush()
    actual = await checkpoint_repo.load_range(session, uid, after_id=0, through_id=rows[-1].id,
                                               include_excluded=True)
    assert [source.content for source in actual] == texts
    expected = [checkpoint.SourceMessage(row.id, row.sender, kind, value, row.created_at)
                for row, value in zip(rows, texts, strict=True)]
    assert checkpoint.source_hash(previous=None, messages=actual) == checkpoint.source_hash(previous=None, messages=expected)
    assert context_retention.source_tokens(rows) == context_retention.source_tokens(expected)
    for row, value in zip(rows, texts, strict=True):
        assert chat._msg_dto(row, "지우")["content"] == value.replace("{유저이름}", "지우")
        assert SourceRef.from_message(row, user_id=uid, generation=0).raw_content_hash == hashlib.sha256(value.encode()).hexdigest()
    assert rows[1].content is None and rows[0].content == texts[0] and not session.dirty
    session.expire_all()
    reloaded = (await session.execute(select(Message).where(Message.user_id == uid).order_by(Message.id))).scalars().all()
    assert reloaded[0].content == texts[0] and reloaded[1].content is None


async def test_greeting_and_response_cipher_and_paired_scrub(checkpoint_db):
    session, (uid, _) = checkpoint_db
    mode = EncryptionMode(True, 1, 1, (1,))
    greeting = Greeting(user_id=uid, context="home_enter", activity_date=date.today(),
        **storage.encode_greeting(user_id=uid, text="합성 첫마디", mode=mode))
    session.add(greeting)
    response = {"synthetic": "합성 응답🙂"}
    row = IdempotencyKey(user_id=uid, key="synthetic-encryption", request_hash="digest", terminal_status="succeeded",
        **storage.encode_response(user_id=uid, key="synthetic-encryption", request_hash="digest", response=response, mode=mode))
    session.add(row)
    await session.flush()
    assert chat._greeting_text(greeting) == "합성 첫마디" and greeting.content is None
    assert chat._replay_payload(row) == response and row.response == {} and not session.dirty
    await session.execute(text("UPDATE idempotency_keys SET response=NULL,response_ciphertext=NULL,redacted_at=now() WHERE user_id=:u"), {"u": uid})
    await session.refresh(row)
    assert row.response is None and row.response_ciphertext is None and chat._replay_payload(row) is None


@pytest.mark.parametrize("expression", [
    "NULL,NULL,NULL", "NULL,decode('0100','hex'),1", "'plaintext',decode(repeat('00',32),'hex'),1",
    "NULL,decode('01000001'||repeat('00',28),'hex'),NULL",
])
async def test_message_checks_reject_null_or_malformed_storage(checkpoint_db, expression):
    session, (uid, _) = checkpoint_db
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(text("INSERT INTO messages(user_id,sender,kind,content,content_ciphertext,content_chars,activity_date) "
                f"VALUES(:u,'user','normal',{expression},CURRENT_DATE)"), {"u": uid})


async def test_expansion_mode_exists_and_share_fence_is_transaction_scoped(checkpoint_db):
    session, _ = checkpoint_db
    mode = await load_mode(session, lock=True)
    assert mode.format_version == 1 and mode.active_key_id in mode.required_key_ids
    # No conversion or per-user adoption metadata is required to read the mode.
    count = (await session.execute(text("SELECT count(*) FROM app_config WHERE key='chat_content_encryption_state'"))).scalar_one()
    assert count == 1


async def test_publish_mode_is_read_again_at_final_transaction(checkpoint_db):
    import uuid
    session, (uid, _) = checkpoint_db
    before = await load_mode(session)
    token = uuid.uuid4()
    lease = Lease(token, 1, 0)
    await session.execute(text("""INSERT INTO chat_active_turns
      (user_id,turn_seq,idempotency_key,request_hash,base_context_revision,lease_token,lease_until)
      VALUES(:u,1,'synthetic-encryption','digest',0,:t,now()+interval '1 minute')"""), {"u": uid, "t": token})
    # The activation is never committed by this test; other clients see no change.
    async with session.begin_nested() as savepoint:
        await activate(session, expected_key_id=1, expected_required_key_ids=(1,), release_sha="a" * 40)
        final = await verify_actual(session, user_id=uid, lease=lease, memory_generation=0, encryption_mode=True)
        assert final.started is True
        values = storage.encode_message(user_id=uid, text="created before final publish", mode=final)
        assert values["content"] is None and values["content_ciphertext"]
        await savepoint.rollback()
    assert await load_mode(session) == before


async def test_an_active_writer_prevents_cutover_until_its_transaction_finishes(checkpoint_db):
    import asyncpg
    import os
    from db.envfile import load_conn, assert_dev_target
    session, _ = checkpoint_db
    await load_mode(session, lock=True)
    path = os.environ["MOLY_CHECKPOINT_ENV_FILE"]
    dsn = load_conn(path)
    assert_dev_target(path, dsn)
    other = await asyncpg.connect(dsn, statement_cache_size=0, command_timeout=3)
    try:
        with pytest.raises(asyncpg.LockNotAvailableError):
            await other.fetchval("SELECT value FROM app_config WHERE key='chat_content_encryption_state' FOR UPDATE NOWAIT")
    finally:
        await other.close()


@pytest.mark.parametrize('retention', [False, True])
@pytest.mark.parametrize('language,fact', [
    ('ko', '새 대화에 저장한 {유저이름}의 확인 코드는 보라달-4729🙂야.'),
    ('en', "The new conversation stores {유저이름}'s verification code VioletMoon-4729🙂."),
    ('ja', '新しい会話に保存した{유저이름}の確認コードは紫月-4729🙂だよ。'),
])
@pytest.mark.parametrize('corrupt', [False, True])
async def test_model_sdk_receives_decrypted_history_and_never_corrupt_ciphertext(
    checkpoint_db, monkeypatch, retention, language, fact, corrupt,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.services import llm, naming
    from app.services.context_provenance import wire_messages
    session, (uid, _) = checkpoint_db
    values = [('user', 'Synthetic legacy conversation.', False),
              ('moly', 'Synthetic legacy reply.', False),
              ('user', fact, True), ('moly', 'Synthetic stored acknowledgment.', True)]
    stored = []
    for sender, body, encrypted in values:
        row = Message(user_id=uid, sender=sender, kind='normal', activity_date=date.today(),
                      created_at=datetime.now(timezone.utc),
                      **storage.encode_message(user_id=uid, text=body,
                          mode=EncryptionMode(encrypted, 1, 1, (1,))))
        session.add(row)
        stored.append(row)
    await session.flush()
    if corrupt:
        damaged = bytearray(stored[2].content_ciphertext)
        damaged[-1] ^= 1
        stored[2].content_ciphertext = bytes(damaged)
        await session.flush()
    create = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='Synthetic completion.'), finish_reason='stop')],
        usage=None, model='gpt-6-luna', id='synthetic-model-transport'))
    monkeypatch.setattr(llm, '_get_openai_client', lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    if corrupt:
        with pytest.raises(content_crypto.ContentCryptoError):
            await chat._context(session, uid, 0, current_text='Repeat the verification code.',
                current_date=date.today(), language=language, compact_crises=False,
                retention_enabled=retention, memory_generation=0)
        create.assert_not_awaited()
        return
    window = await chat._context(session, uid, 0, current_text='Repeat the verification code.',
        current_date=date.today(), language=language, compact_crises=False,
        retention_enabled=retention, memory_generation=0)
    for entry in window.conversation:
        entry['content'] = naming.render(entry['content'], 'Kai')
    await llm.generate('Synthetic model input verification.', wire_messages(window.conversation),
                       model='gpt-6-luna')
    create.assert_awaited_once()
    outgoing = create.await_args.kwargs['messages']
    assert all(set(message) == {'role', 'content'} and isinstance(message['content'], str)
               for message in outgoing)
    assert any('Synthetic legacy conversation.' in message['content'] for message in outgoing)
    assert any(fact.replace('{유저이름}', 'Kai') in message['content'] for message in outgoing)
    assert all('content_ciphertext' not in message and 'content_chars' not in message for message in outgoing)
    assert stored[2].content is None and stored[2].content_ciphertext and not session.dirty
