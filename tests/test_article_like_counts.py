"""Exercise aggregate queries against SQL, including personal/global separation."""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.article import ArticleProgress
from app.services import articles


class AsyncReadSession:
    def __init__(self, session):
        self.session = session

    async def scalars(self, statement):
        return self.session.scalars(statement)

    async def execute(self, statement):
        return self.session.execute(statement)


@pytest.mark.asyncio
async def test_global_likes_do_not_leak_other_users_progress():
    engine = create_engine('sqlite://')
    ArticleProgress.__table__.create(engine)
    current, other, third = (uuid.uuid4() for _ in range(3))
    with Session(engine) as db:
        db.add_all([
            ArticleProgress(user_id=other, article_id='sample-1', feedback='helpful', read_at=datetime.now(timezone.utc)),
            ArticleProgress(user_id=third, article_id='sample-1', feedback='helpful', read_at=datetime.now(timezone.utc)),
            ArticleProgress(user_id=current, article_id='sample-2', feedback='not_helpful', read_at=datetime.now(timezone.utc)),
        ])
        db.commit()
        adapter = AsyncReadSession(db)
        items = (await articles.list_activity(adapter, str(current)))['articles']
        assert [a['like_count'] for a in items] == [2, 0, 0, 0, 0]
        assert not items[0]['is_read'] and items[0]['feedback'] is None
        assert items[1]['is_read'] and items[1]['feedback'] == 'not_helpful'
        row = db.get(ArticleProgress, (other, 'sample-1'))
        row.feedback = 'helpful'
        db.commit()
        assert (await articles.list_activity(adapter, str(current)))['articles'][0]['like_count'] == 2
        row.feedback = 'not_helpful'
        db.commit()
        assert (await articles.list_activity(adapter, str(current)))['articles'][0]['like_count'] == 1
        db.delete(row)
        db.commit()
        assert (await articles.list_activity(adapter, str(current)))['articles'][0]['like_count'] == 1
    engine.dispose()
