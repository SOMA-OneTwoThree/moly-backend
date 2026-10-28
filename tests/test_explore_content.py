"""Server-owned articles: seed, rendering and validation."""
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_session
from app.core.errors import AppError
from app.core.security import get_current_user
from app.main import app
from app.schemas.articles import ArticleContentList
from app.services import articles

DB = Path(__file__).resolve().parents[1] / "db"


def seeded(name, path):
    match = re.search(rf"\${name}\$\n(.*?)\n\${name}\$", (DB / path).read_text(encoding="utf-8"), re.S)
    return json.loads(match.group(1))


def test_seed_is_identical_for_new_and_existing_databases():
    assert seeded("articles", "seed.sql") == seeded("articles", "changes/explore_content.sql")
    assert seeded("articles", "seed.sql") == seeded("articles", "changes/explore_prod.sql")


def test_article_seed_matches_the_app_manuscript():
    rows = seeded("articles", "seed.sql")
    assert [r["id"] for r in rows] == [f"sample-{i}" for i in range(1, 6)]
    assert [r["sort_order"] for r in rows] == [0, 1, 2, 3, 4]
    assert all(r["is_active"] and set(r["content_i18n"]) == {"ko"} for r in rows)
    assert [(r["cover"], r["read_minutes"], r["preview_block_count"]) for r in rows] == [
        ("mug", 3, None), ("sofa", 4, 1), ("night", 4, 1), ("tasks", 3, 2), ("tea", 5, 1)]
    ko = [r["content_i18n"]["ko"] for r in rows]
    assert [len(c["blocks"]) for c in ko] == [31, 31, 31, 31, 30]
    assert sum(len(b["highlights"]) for c in ko for b in c["blocks"]) == 10
    assert ko[0]["title"] == "나에게만 유독 엄격한 당신에게"
    assert ko[4]["summary"] == "나를 잃지 않으면서도 관계가 편안해지는 거절 연습"
    first = ko[0]["blocks"][0]
    assert first == {"kind": "paragraph", "highlights": [], "text": (
        "가장 친한 친구가 실수를 해서 풀죽어 있을 때, 우리는 어떤 말을 건네나요? "
        "아마 \"누구나 그럴 수 있어, 괜찮아\"라며 따뜻하게 위로해줄 거예요.")}
    assert ko[0]["blocks"][3]["highlights"] == ["소중한 친구를 대하듯 나를 따뜻하게 이해해 주는 태도"]
    assert ko[0]["blocks"][13]["text"].startswith("1️⃣ 친구에게 해주듯 말 바꿔보기\n내가 실수를")
    assert ko[4]["blocks"][-1]["highlights"] == ["내 마음이 괜찮은지 먼저 물어봐 주기로 해요!"]
    rendered = ArticleContentList.model_validate(
        {"articles": [articles._render(SimpleNamespace(**r), "ja") for r in rows]})
    assert rendered.articles[1].blocks[0].kind == "paragraph"


def article(id, **content_i18n):
    return SimpleNamespace(id=id, cover="mug", read_minutes=3, preview_block_count=None,
                           content_i18n=content_i18n)


def body(title):
    return {"title": title, "summary": "s", "blocks": [{"kind": "heading", "text": title}]}


async def test_listing_uses_requested_locale_then_korean():
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = [article("b", ko=body("가"), en=body("A")), article("a", ko=body("나"))]
    session.scalars.return_value = result
    listed = await articles.list_articles(session, "en-US")
    assert [(a["id"], a["title"]) for a in listed["articles"]] == [("b", "A"), ("a", "나")]
    assert listed["articles"][0]["blocks"] == [{"kind": "heading", "text": "A", "highlights": []}]
    assert [a["title"] for a in (await articles.list_articles(session, "ja"))["articles"]] == ["가", "나"]
    sql = str(session.scalars.call_args.args[0])
    assert "articles.is_active IS true" in sql and "ORDER BY articles.sort_order, articles.id" in sql


async def test_activity_lists_active_table_ids_in_order():
    session = AsyncMock()
    ids, progress = MagicMock(), MagicMock()
    ids.all.return_value = ["sample-3", "sample-1"]
    progress.all.return_value = [SimpleNamespace(article_id="sample-1", feedback="helpful", rewarded_at=None)]
    session.scalars.side_effect = [ids, progress]
    counts = MagicMock()
    counts.all.return_value = [("sample-1", 4)]
    session.execute.return_value = counts
    listed = (await articles.list_activity(session, "00000000-0000-4000-8000-000000000001"))["articles"]
    assert [(a["article_id"], a["is_read"], a["like_count"]) for a in listed] == [
        ("sample-3", False, 0), ("sample-1", True, 4)]
    assert "articles.is_active IS true" in str(session.scalars.call_args_list[0].args[0])


@pytest.mark.parametrize("call", [{}, {"feedback": "helpful"}, {"clear_feedback": True}])
async def test_unknown_or_inactive_article_is_not_found_before_any_write(call):
    session = AsyncMock()
    result = MagicMock()
    result.one_or_none.return_value = None
    session.execute.return_value = result
    with pytest.raises(AppError) as error:
        await articles.record(session, "00000000-0000-4000-8000-000000000001", "sample-6", **call)
    assert (error.value.code, error.value.http_status) == ("NOT_FOUND", 404)
    assert "articles.is_active IS true" in str(session.execute.call_args.args[0])
    session.get.assert_not_called()
    session.commit.assert_not_called()


def test_articles_require_auth():
    assert TestClient(app).get("/articles").status_code == 401


@pytest.fixture
def signed_in():
    async def session():
        yield None
    app.dependency_overrides[get_current_user] = lambda: "account-from-token"
    app.dependency_overrides[get_session] = session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_articles_pass_locale(signed_in, monkeypatch):
    calls = []
    async def list_articles(db, locale):
        calls.append(locale)
        return {"articles": []}
    monkeypatch.setattr(articles, "list_articles", list_articles)
    assert signed_in.get("/articles", headers={"X-App-Locale": "en"}).json() == {"articles": []}
    assert calls == ["en"]


def test_production_cutover_preserves_all_ledger_types_and_is_one_transaction():
    cutover = (DB / "changes/explore_prod.sql").read_text()
    baseline = (DB / "schema.sql").read_text()
    pattern = r"hay_transactions_type_check CHECK.*?ARRAY\[(.*?)\]"
    expected = set(re.findall(r"'([^']+)'::text", re.search(pattern, baseline, re.S).group(1)))
    actual = set(re.findall(r"'([^']+)'::text", re.search(pattern, cutover, re.S).group(1)))
    assert actual == expected
    assert {"mood_reward", "training_reward", "article_reward"} <= actual
    assert not re.search(r"^\s*(?:BEGIN|COMMIT);", cutover, re.M | re.I)
