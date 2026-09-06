"""No database transaction is held while the manual compiler calls a provider."""
from types import SimpleNamespace
import uuid

from scripts import backfill_interaction_contracts as backfill


async def test_compiler_releases_read_session_before_external_call(monkeypatch):
    opened = False
    class Session:
        async def __aenter__(self):
            nonlocal opened
            opened = True
            return self
        async def __aexit__(self, *args):
            nonlocal opened
            opened = False
        async def execute(self, statement, params):
            if statement is backfill._MESSAGES:
                return SimpleNamespace(all=lambda: [(1, 'user', 'call me friend')])
            return SimpleNamespace(first=lambda: ('tester', 'en'))
    async def lock(session, uid):
        assert opened
        return 4
    async def generate(*args, **kwargs):
        assert not opened
        return SimpleNamespace(text='[]')
    monkeypatch.setattr(backfill, 'lock_active_subject', lock)
    monkeypatch.setattr(backfill.llm, 'generate', generate)
    kept, dropped, locale, epoch, source = await backfill._compile_for_user(Session, uuid.uuid4())
    assert (kept, dropped, locale, epoch, source) == ([], [], 'en', 4, [(1, 'user', 'call me friend')])
