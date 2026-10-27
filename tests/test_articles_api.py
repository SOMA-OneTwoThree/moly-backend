from fastapi.testclient import TestClient
import pytest
from app.main import app
from app.core.security import get_current_user
from app.core.db import get_session
from app.services import articles

@pytest.mark.parametrize('method,path', [('GET','/article-progress'),('PUT','/articles/sample-1/feedback'),('DELETE','/articles/sample-1/feedback'),('POST','/articles/sample-1/reward')])
def test_article_routes_require_auth(method,path):
    assert TestClient(app).request(method,path,json={}).status_code == 401

@pytest.mark.parametrize('payload',[{}, {'feedback':'unknown'}, {'feedback':'helpful','user_id':'other'}])
def test_feedback_rejects_invalid_payload(payload):
    async def session(): yield None
    app.dependency_overrides[get_current_user] = lambda:'00000000-0000-4000-8000-000000000001'
    app.dependency_overrides[get_session] = session
    try:
        assert TestClient(app).put('/articles/sample-1/feedback',json=payload).status_code == 422
    finally:
        app.dependency_overrides.clear()

def test_reward_response_and_identity(monkeypatch):
    calls=[]
    async def session(): yield None
    async def record(db, uid, article_id, **kw):
        calls.append((uid,article_id))
        return dict(article_id=article_id,feedback=None,is_read=True,reward_claimed=True,granted=20,balance_after=20,like_count=0)
    monkeypatch.setattr(articles,'record',record)
    app.dependency_overrides[get_current_user] = lambda:'account-from-token'
    app.dependency_overrides[get_session] = session
    try:
        r=TestClient(app).post('/articles/sample-1/reward')
        assert r.status_code==200 and r.json()['granted']==20
        assert calls==[('account-from-token','sample-1')]
    finally:
        app.dependency_overrides.clear()


def test_clear_feedback_calls_record_with_clear_flag(monkeypatch):
    calls=[]
    async def session(): yield None
    async def record(db, uid, article_id, **kw):
        calls.append((uid,article_id,kw))
        return dict(article_id=article_id,feedback=None,is_read=True,reward_claimed=False,granted=0,balance_after=None,like_count=0)
    monkeypatch.setattr(articles,'record',record)
    app.dependency_overrides[get_current_user] = lambda:'account-from-token'
    app.dependency_overrides[get_session] = session
    try:
        r=TestClient(app).delete('/articles/sample-1/feedback')
        assert r.status_code==200 and r.json()['feedback'] is None
        assert calls==[('account-from-token','sample-1',{'clear_feedback':True})]
    finally:
        app.dependency_overrides.clear()
