"""Article SQL behavior: like counts, reward entitlement and mutation boundaries."""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.article import ArticleProgress
from app.services import articles


class AsyncReadSession:
    def __init__(self, session):
        self.session = session

    async def scalars(self, statement):
        return self.session.scalars(statement)

    async def execute(self, statement):
        return self.session.execute(statement)


class AsyncWriteSession(AsyncReadSession):
    """Use the existing in-memory SQL fixture without a network DB or wallet calls."""

    def __init__(self, session):
        super().__init__(session)
        self.added = []
        self.flushes = 0
        self.commits = 0

    async def get(self, model, key):
        return self.session.get(model, key)

    async def scalar(self, statement):
        return self.session.scalar(statement)

    def add(self, row):
        self.added.append(row)
        self.session.add(row)

    async def flush(self):
        self.flushes += 1
        self.session.flush()

    async def commit(self):
        self.commits += 1
        self.session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("article_id,index", [("sample-1", 0), ("sample-6", 5), ("sample-7", 6)])
async def test_global_likes_do_not_leak_other_users_progress(article_id, index):
    engine = create_engine('sqlite://')
    ArticleProgress.__table__.create(engine)
    with engine.begin() as conn:
        # Only the columns the activity query reads; sample-8 is retired.
        conn.execute(text('CREATE TABLE articles (id TEXT PRIMARY KEY, sort_order INTEGER, is_active BOOLEAN)'))
        conn.execute(text('INSERT INTO articles VALUES ' + ','.join(
            f"('sample-{i}', {i}, {int(i < 8)})" for i in range(1, 9))))
    current, other, third = (uuid.uuid4() for _ in range(3))
    with Session(engine) as db:
        db.add_all([
            ArticleProgress(user_id=other, article_id=article_id, feedback='helpful', read_at=datetime.now(timezone.utc)),
            ArticleProgress(user_id=third, article_id=article_id, feedback='helpful', read_at=datetime.now(timezone.utc)),
            ArticleProgress(user_id=current, article_id='sample-2', feedback='not_helpful', read_at=datetime.now(timezone.utc)),
        ])
        db.commit()
        adapter = AsyncReadSession(db)
        items = (await articles.list_activity(adapter, str(current)))['articles']
        assert [a['like_count'] for a in items] == [2 if i == index else 0 for i in range(7)]
        assert not items[index]['is_read'] and items[index]['feedback'] is None
        assert items[1]['is_read'] and items[1]['feedback'] == 'not_helpful'
        row = db.get(ArticleProgress, (other, article_id))
        row.feedback = 'helpful'
        db.commit()
        assert (await articles.list_activity(adapter, str(current)))['articles'][index]['like_count'] == 2
        row.feedback = 'not_helpful'
        db.commit()
        assert (await articles.list_activity(adapter, str(current)))['articles'][index]['like_count'] == 1
        db.delete(row)
        db.commit()
        assert (await articles.list_activity(adapter, str(current)))['articles'][index]['like_count'] == 1
    engine.dispose()


# Letters keep SQLite's UUID numeric affinity from coercing this fixture to a float.
UID = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
NOW = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def article_store(monkeypatch):
    engine = create_engine('sqlite://')
    ArticleProgress.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE articles (id TEXT PRIMARY KEY, is_active BOOLEAN, preview_block_count INTEGER)'))
        conn.execute(text("INSERT INTO articles VALUES ('sample', 1, 1)"))
        conn.execute(text('CREATE TABLE profiles (id TEXT PRIMARY KEY, hay_balance INTEGER)'))
        conn.execute(text('INSERT INTO profiles VALUES (:uid, 100)'), {'uid': UID.hex})
    with Session(engine) as db:
        session = AsyncWriteSession(db)
        state = SimpleNamespace(session=session, db=db, plan='free', ledger=[], events=[],
                                entitlement_calls=[], failure=None, deleting=False, clock=NOW,
                                lock_acquired_at=NOW, during_lock=None)

        async def lock(session, uid):
            assert uid == UID
            state.events.append('lock')
            state.clock = state.lock_acquired_at
            if state.during_lock is not None:
                state.during_lock()

        async def ensure(session, uid):
            assert uid == UID
            state.events.append('privacy')
            if state.deleting:
                raise AppError('ACCOUNT_DELETING', 409, '계정 삭제를 처리하고 있어요.')

        async def entitlement(session, user_id, now):
            assert user_id == str(UID)
            state.events.append('entitlement')
            state.entitlement_calls.append(now)
            if state.failure is not None:
                raise state.failure
            # A spent chat allowance must not block a subscriber's reward.
            return {'plan': state.plan, 'tokens_remaining': 0}

        async def apply(session, uid, tx_type, amount):
            state.ledger.append((uid, tx_type, amount))
            db.execute(text('UPDATE profiles SET hay_balance = hay_balance + :amount WHERE id = :uid'),
                       {'amount': amount, 'uid': uid.hex})
            return SimpleNamespace(balance_after=db.scalar(text('SELECT hay_balance FROM profiles')))

        class Clock:
            @staticmethod
            def now(tz):
                return state.clock

        monkeypatch.setattr(articles, 'advisory_xact_lock', lock)
        monkeypatch.setattr(articles.privacy, 'ensure_subject_active', ensure)
        monkeypatch.setattr(articles.gating, 'resolve_entitlement', entitlement)
        monkeypatch.setattr(articles.hay_ledger, 'apply', apply)
        monkeypatch.setattr(articles, 'datetime', Clock)
        yield state
    engine.dispose()


def _set_preview(store, value):
    store.db.execute(text('UPDATE articles SET preview_block_count = :value'), {'value': value})
    store.db.commit()


def _progress(store):
    return store.db.get(ArticleProgress, (UID, 'sample'))


def _assert_no_reward_write(store):
    assert store.ledger == []
    assert store.session.added == []
    assert store.session.flushes == store.session.commits == 0
    assert not store.db.new and not store.db.dirty
    assert store.db.scalar(text('SELECT hay_balance FROM profiles')) == 100


@pytest.mark.asyncio
@pytest.mark.parametrize('preview', [0, 1, 100])
@pytest.mark.parametrize('existing_feedback', [None, 'helpful'])
async def test_free_premium_reward_changes_nothing(article_store, preview, existing_feedback):
    store = article_store
    _set_preview(store, preview)
    if existing_feedback:
        store.db.add(ArticleProgress(user_id=UID, article_id='sample', feedback=existing_feedback, read_at=NOW))
        store.db.commit()
    previous = _progress(store)
    previous_read_at = previous.read_at if previous else None
    with pytest.raises(AppError) as exc:
        await articles.record(store.session, str(UID), 'sample')
    assert (exc.value.code, exc.value.http_status, exc.value.details) == ('ARTICLE_SUBSCRIPTION_REQUIRED', 403, {})
    _assert_no_reward_write(store)
    row = _progress(store)
    if existing_feedback:
        assert (row.feedback, row.read_at, row.rewarded_at) == (existing_feedback, previous_read_at, None)
    else:
        assert row is None
    assert store.events == ['lock', 'privacy', 'entitlement']


@pytest.mark.asyncio
@pytest.mark.parametrize('plan,preview', [('free', None), ('trial', 0), ('monthly', 1), ('yearly', 100)])
async def test_eligible_reward_is_once_and_replay_survives_expiry(article_store, plan, preview):
    store = article_store
    store.plan = plan
    _set_preview(store, preview)
    first = await articles.record(store.session, str(UID), 'sample')
    assert first['granted'] == 20 and first['balance_after'] == 120
    assert first['reward_claimed'] and first['is_read']
    calls = len(store.entitlement_calls)
    assert calls == (0 if preview is None else 1)
    # Article publication may change after payout, and the user may lose access.
    _set_preview(store, 0)
    store.plan = 'free'
    store.failure = AssertionError('replay must not resolve entitlement')
    store.db.execute(text('UPDATE profiles SET hay_balance = 157'))
    store.db.commit()
    replay = await articles.record(store.session, str(UID), 'sample')
    assert replay['granted'] == 0 and replay['balance_after'] == 157
    assert replay['reward_claimed'] and len(store.ledger) == 1
    assert len(store.entitlement_calls) == calls


@pytest.mark.asyncio
async def test_free_premium_feedback_and_clear_keep_read_and_reward_history(article_store):
    store = article_store
    store.failure = AssertionError('feedback must not resolve entitlement')
    result = await articles.record(store.session, str(UID), 'sample', feedback='helpful')
    read_at = _progress(store).read_at
    assert result['feedback'] == 'helpful' and result['like_count'] == 1
    assert result['is_read'] and not result['reward_claimed'] and result['granted'] == 0
    result = await articles.record(store.session, str(UID), 'sample', feedback='not_helpful')
    assert result['feedback'] == 'not_helpful' and result['like_count'] == 0
    # Clearing feedback must retain a historical payout, even after expiry.
    row = _progress(store)
    row.rewarded_at = NOW
    store.db.commit()
    result = await articles.record(store.session, str(UID), 'sample', clear_feedback=True)
    assert result['feedback'] is None and result['is_read'] and result['reward_claimed']
    assert _progress(store).read_at == read_at
    assert result['granted'] == 0 and store.ledger == [] and store.entitlement_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('retired', [False, True])
async def test_missing_or_inactive_article_stays_not_found(article_store, retired):
    store = article_store
    if retired:
        store.db.execute(text('UPDATE articles SET is_active = 0'))
        store.db.commit()
    with pytest.raises(AppError) as exc:
        await articles.record(store.session, str(UID), 'sample' if retired else 'missing')
    assert (exc.value.code, exc.value.http_status) == ('NOT_FOUND', 404)
    _assert_no_reward_write(store)
    assert store.events == ['lock']


@pytest.mark.asyncio
@pytest.mark.parametrize('deactivate', [False, True])
async def test_article_access_is_read_after_waiting_for_the_lock(article_store, deactivate):
    store = article_store
    _set_preview(store, None)

    def change_article():
        if deactivate:
            store.db.execute(text('UPDATE articles SET is_active = 0'))
            store.db.commit()
        else:
            _set_preview(store, 0)

    store.during_lock = change_article
    with pytest.raises(AppError) as exc:
        await articles.record(store.session, str(UID), 'sample')
    assert (exc.value.code, exc.value.http_status) == (
        ('NOT_FOUND', 404) if deactivate else ('ARTICLE_SUBSCRIPTION_REQUIRED', 403)
    )
    _assert_no_reward_write(store)
    assert _progress(store) is None
    assert store.events == (['lock'] if deactivate else ['lock', 'privacy', 'entitlement'])


@pytest.mark.asyncio
@pytest.mark.parametrize('kwargs', [{}, {'feedback': 'helpful'}, {'clear_feedback': True}])
@pytest.mark.parametrize('rewarded', [False, True])
async def test_deleting_account_blocks_all_article_writes_and_replay(article_store, kwargs, rewarded):
    store = article_store
    if rewarded:
        store.db.add(ArticleProgress(user_id=UID, article_id='sample', read_at=NOW, rewarded_at=NOW))
        store.db.commit()
    store.deleting = True
    with pytest.raises(AppError) as exc:
        await articles.record(store.session, str(UID), 'sample', **kwargs)
    assert (exc.value.code, exc.value.http_status) == ('ACCOUNT_DELETING', 409)
    _assert_no_reward_write(store)
    assert store.events == ['lock', 'privacy']


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [AppError('NOT_FOUND', 404, '프로필을 찾을 수 없어요.'), RuntimeError('query failed')])
async def test_entitlement_failure_propagates_without_a_read_or_reward(article_store, failure):
    store = article_store
    store.failure = failure
    with pytest.raises(type(failure)) as exc:
        await articles.record(store.session, str(UID), 'sample')
    assert exc.value is failure
    _assert_no_reward_write(store)
    assert _progress(store) is None


@pytest.mark.asyncio
async def test_reward_entitlement_uses_time_after_lock_acquisition(article_store):
    store = article_store
    store.lock_acquired_at = datetime(2026, 10, 28, 9, 1, tzinfo=timezone.utc)
    with pytest.raises(AppError) as exc:
        await articles.record(store.session, str(UID), 'sample')
    assert exc.value.code == 'ARTICLE_SUBSCRIPTION_REQUIRED'
    assert store.entitlement_calls == [store.lock_acquired_at]
    _assert_no_reward_write(store)
