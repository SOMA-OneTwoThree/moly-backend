"""Existing development Supabase only; all synthetic rows roll back."""
from datetime import date, timedelta

import pytest

from app.core import errors
from app.models.greeting import Greeting
from app.services import chat, greeting_context, message_content
from app.services.chat_content_encryption import EncryptionMode
from tests.integration.test_checkpoint_continuity_dev import checkpoint_db as _checkpoint_db

checkpoint_db = _checkpoint_db


@pytest.mark.parametrize("encrypted", [False, True])
async def test_actual_dev_greeting_read_wire_and_revalidation(checkpoint_db, encrypted):
    session, (uid, other) = checkpoint_db
    stored = "{유저이름}, 밥 먹고 걷다가 왔어."
    row = Greeting(user_id=uid, context="home_enter", activity_date=date.today(),
        **message_content.encode_greeting(user_id=uid, text=stored,
            mode=EncryptionMode(encrypted, 1, 1, (1,))))
    session.add(row)
    await session.flush()
    gid = row.id
    session.expire_all()
    opening, loaded = await chat._read_opening(session, uid, gid, "지우")
    assert opening.text == stored.replace("{유저이름}", "지우")
    assert (loaded.content is None) is encrypted
    current = {"role": "user", "content": "나도"}
    wire = greeting_context.attach([{"role": "assistant", "content": "어제 아침 메뉴"}, current],
                                   opening, user_id=uid)
    assert wire[-2]["content"] == opening.text and wire[-1] is current
    assert await chat._revalidate_opening(session, uid, opening, "지우") is loaded
    assert (await chat._read_opening(session, other, gid, "다른이름"))[0] is None
    loaded.activity_date += timedelta(days=1)
    await session.flush()
    with pytest.raises(errors.AppError) as caught:
        await chat._revalidate_opening(session, uid, opening, "지우")
    assert caught.value.code == "CHAT_TURN_STALE"
