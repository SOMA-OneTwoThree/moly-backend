"""Development harness offline contracts. No network, DB, or provider clients."""
import json
from types import SimpleNamespace
import uuid

import pytest

from app.core import content_crypto
from app.services import message_content
from scripts import replay_guard_context_dev as replay
from scripts import smoke_chat_encryption_dev as smoke
from tests.test_content_crypto import ring
from tests.test_idempotency_response_verifier import VALID

UID = uuid.UUID(int=1)
MODE = SimpleNamespace(started=True, active_key_id=1, format_version=1, required_key_ids=(1,))


def test_default_harness_is_offline_and_does_not_open_environment(monkeypatch, capsys):
    async def forbidden(*args, **kwargs):
        raise AssertionError('execution started')

    monkeypatch.setattr(smoke, 'run', forbidden)
    monkeypatch.setattr(smoke.logging, 'disable', lambda *args: None)
    assert smoke.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['network_calls'] == 0 and report['planned_turns'] == 36
    assert report['synthetic_accounts'] == 3


@pytest.mark.parametrize('language', ['ko', 'en', 'ja'])
def test_corpus_has_strict_routes_replay_and_long_normal_worker_sources(language):
    steps = smoke.steps(language)
    assert len(steps) == 12 and tuple(step['kind'] for step in steps) == smoke.KINDS
    assert all(0 < len(step['text']) <= 2000 and step['replay'] for step in steps)
    assert sum(len(step['text'].encode()) for step in steps if step['kind'] == 'normal') >= 4096
    # B's actual question at step 5 is no longer in last six raw messages
    # before step 10; a distinct guide D at step 8 must not erase dedup B.
    assert steps[4]['kind'] == 'safety_check' and steps[7]['kind'] == 'safety'
    assert steps[9]['kind'] == 'normal' and steps[10]['kind'] == 'safety_check'
    assert steps[2]['kind'] == steps[3]['kind'] == steps[11]['kind'] == 'safety'


async def test_raw_immutable_proof_binds_all_three_storage_fields():
    queries = []

    class DB:
        async def fetch(self, sql, uid, ids):
            queries.append(sql)
            return [{'id': 7, 'sender': 'user', 'kind': 'normal', 'content_hash': 'a' * 64}]

    runner = replay.Runner(DB(), None, {}, '', 'a' * 40, lambda _: None)
    assert await runner.raw_message_proofs(UID, [7]) == {7: ('user', 'normal', 'a' * 64)}
    assert "jsonb_build_array(content,encode(content_ciphertext,'hex'),content_chars)" in queries[0]
    assert 'sha256' in queries[0] and 'COALESCE(content' not in queries[0]


@pytest.mark.parametrize('corrupt', [None, 'plaintext_copy', 'missing_cipher', 'bad_chars', 'cache_plain', 'cache_bad_owner'])
async def test_actual_turn_verifier_rejects_new_plaintext_and_bad_encrypted_shapes(monkeypatch, corrupt):
    monkeypatch.setattr(content_crypto, '_cached_keyring', ring())
    body, reply = 'synthetic input🙂', 'synthetic reply'
    payload = json.loads(json.dumps(VALID))
    payload['reply']['content'] = reply
    rows = [dict(id=1, sender='user', kind='normal', **message_content.encode_message(user_id=UID, text=body, mode=MODE)),
            dict(id=2, sender='moly', kind='normal', **message_content.encode_message(user_id=UID, text=reply, mode=MODE))]
    encrypted = message_content.encode_response(user_id=UID, key='request', request_hash='hash', response=payload, mode=MODE)
    cache = dict(request_hash='hash', redacted_at=None, terminal_status='succeeded', **encrypted)
    if corrupt == 'plaintext_copy':
        rows[0]['content'] = body
    elif corrupt == 'missing_cipher':
        rows[0]['content_ciphertext'] = None
    elif corrupt == 'bad_chars':
        rows[0]['content_chars'] += 1
    elif corrupt == 'cache_plain':
        cache['response'] = payload
    elif corrupt == 'cache_bad_owner':
        cache.update(message_content.encode_response(user_id=uuid.UUID(int=2), key='request',
                                                     request_hash='hash', response=payload, mode=MODE))

    class DB:
        async def fetch(self, sql, *params):
            return rows

        async def fetchval(self, *args):
            return 'synthetic'

        async def fetchrow(self, *args):
            return cache

    runner = smoke.EncryptionRunner(DB(), None, {}, '', 'a' * 40, lambda _: None)
    record = {'payload': payload, 'body': {'text': body}, 'key': 'request'}
    if corrupt is None:
        await runner.verify_turn_storage(UID, record)
        assert rows[0]['content'] is None
    else:
        with pytest.raises((replay.ReplayError, message_content.ContentCryptoError)):
            await runner.verify_turn_storage(UID, record)
