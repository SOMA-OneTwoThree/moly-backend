"""관계 프로필 저장소(W9) — publish 계약 / locale당 published 1개 / terminal 불가역 / cross-user.

실 DB에 붙지 않는다. `_FakeSession`이 문장을 SQL 텍스트로 식별해 캔 결과를 돌려주고 실행 순서를
기록한다(`test_memory_repo`와 같은 방식). 시뮬레이터만으로는 SQL을 바꿔도 통과하므로, 문장 자체의
불변식(출발 상태 WHERE·되살리는 UPDATE 부재·유저 스코프)과 마이그레이션의 부분 유니크 인덱스는
구조로 따로 못 박는다.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.services import relationship_profile as rp
from app.services import relationship_profile_repo as repo

_UID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER = uuid.UUID("99999999-9999-9999-9999-999999999999")
_FACT = "aaaaaaaa-1111-1111-1111-111111111111"
_FACT_OTHER_USER = "bbbbbbbb-2222-2222-2222-222222222222"
_INSIGHT = "cccccccc-3333-3333-3333-333333333333"
_DRAFT_ID = uuid.UUID("dddddddd-4444-4444-4444-444444444444")
_PUBLISHED_ID = uuid.UUID("eeeeeeee-5555-5555-5555-555555555555")

_MIGRATION = pathlib.Path(__file__).resolve().parents[1] / "db/migrations/20260804_relationship_profiles.sql"


def _item(key: str, text: str = "본문", refs=None) -> rp.ProfileItem:
    return rp.build_item(
        item_key=key, text=text, source_refs=refs or [{"type": "fact", "id": _FACT}]
    )


def _document() -> rp.ProfileDocument:
    return rp.build_document(stance=_item("s"), known_facts=[_item("k")])


def _edges_of(document: rp.ProfileDocument) -> list[dict]:
    return [
        {
            "item_key": item.item_key,
            "fact_id": uuid.UUID(ref.id) if ref.type == rp.SOURCE_FACT else None,
            "insight_id": uuid.UUID(ref.id) if ref.type == rp.SOURCE_INSIGHT else None,
        }
        for item in document.items
        for ref in item.source_refs
    ]


class _Res:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def scalar_one(self):
        return self._rows[0][0]


class _FakeSession:
    """repo가 실행하는 문장을 기록하고 캔 결과를 돌려준다. commit은 하지 않는다(호출측 경계)."""

    def __init__(self, **canned) -> None:
        self.calls: list[tuple[str, object]] = []
        self.committed = False
        doc = canned.get("document", _document())
        self.document = doc
        self.context = canned.get("context", (0, 0))
        self.draft = canned.get(
            "draft",
            {
                "id": _DRAFT_ID,
                "version": 2,
                "locale": "ko",
                "memory_generation": 0,
                "relationship_profile_input_revision": 0,
                "document_json": doc.as_json(),
                "render_hash": "draft-hash",
                "status": "draft",
            },
        )
        self.published = canned.get("published")  # dict | None
        self.edges = canned.get("edges", _edges_of(doc))
        self.usable_facts = canned.get("usable_facts", {_FACT})
        self.usable_insights = canned.get("usable_insights", {_INSIGHT})
        self.supersede_hits = canned.get("supersede_hits", True)
        self.publish_hits = canned.get("publish_hits", True)
        self.next_version = canned.get("next_version", 3)
        self.new_profile_id = uuid.uuid4()

    @property
    def statements(self) -> list[str]:
        return [c[0] for c in self.calls]

    def executed(self, sql) -> list[object]:
        return [p for s, p in self.calls if s == str(sql)]

    def ran(self, sql) -> bool:
        return str(sql) in self.statements

    async def execute(self, stmt, params=None):
        s = str(stmt)
        self.calls.append((s, params))
        if s in (str(repo._CONTEXT_SQL), str(repo._LOCK_CONTEXT_SQL)):
            return _Res([self.context] if self.context is not None else [])
        if s == str(repo._DRAFT_SQL):
            return _Res([self.draft] if self.draft is not None else [])
        if s in (str(repo._PUBLISHED_SQL), str(repo._LOCK_PUBLISHED_SQL)):
            return _Res([self.published] if self.published is not None else [])
        if s == str(repo._EDGES_SQL):
            return _Res(self.edges)
        if s == str(repo._USABLE_FACTS_SQL):  # 유저 스코프 쿼리의 결과를 흉내 — 없는 id는 안 돌아온다
            return _Res([(i,) for i in params["ids"] if str(i) in self.usable_facts])
        if s == str(repo._USABLE_INSIGHTS_SQL):
            return _Res([(i,) for i in params["ids"] if str(i) in self.usable_insights])
        if s == str(repo._NEXT_VERSION_SQL):
            return _Res([(self.next_version,)])
        if s == str(repo._INSERT_DRAFT_SQL):
            return _Res([(self.new_profile_id,)])
        if s == str(repo._SUPERSEDE_PUBLISHED_SQL):
            return _Res([(params["profile_id"],)] if self.supersede_hits else [])
        if s == str(repo._PUBLISH_DRAFT_SQL):
            return _Res([(params["profile_id"],)] if self.publish_hits else [])
        if s in (str(repo._INSERT_EDGE_SQL), str(repo._INVALIDATE_DRAFT_SQL)):
            return _Res([])
        raise AssertionError(f"시뮬레이터가 모르는 문장: {s[:90]}")

    async def commit(self):  # pragma: no cover - 저장소는 커밋하지 않는다
        self.committed = True
        raise AssertionError("저장소가 커밋하면 안 된다 — 호출측 트랜잭션 경계에 합류한다")


# ─────────────────────────────────────────────────────────────
# locale당 published 정확히 1개 — 마지막 방어선은 DB 인덱스다.
# ─────────────────────────────────────────────────────────────
def test_migration_declares_the_partial_unique_index():
    sql = "".join(_MIGRATION.read_text().split())
    assert "CREATEUNIQUEINDEXIFNOTEXISTSrelationship_profiles_one_published_idx" in sql
    assert "ONpublic.relationship_profiles(user_id,locale)WHEREstatus='published'" in sql


def test_migration_scopes_source_fks_by_user():
    """단일 컬럼 FK면 A 유저 프로필이 B 유저 사실을 근거로 삼는 경로가 열린다."""
    sql = "".join(_MIGRATION.read_text().split())
    assert "FOREIGNKEY(user_id,fact_id)REFERENCESpublic.memory_facts(user_id,id)ONDELETERESTRICT" in sql
    assert (
        "FOREIGNKEY(user_id,insight_id)REFERENCESpublic.memory_insights(user_id,id)ONDELETERESTRICT"
        in sql
    )
    assert "REVOKEALLONpublic.%IFROManon,authenticated" in sql  # PII 파생 — 클라 직접 접근 차단


async def test_concurrent_publish_for_one_locale_fails_instead_of_making_two_published():
    """DB의 부분 유니크 인덱스가 두 번째 publish를 막는다 — 저장소가 그 오류를 삼키지 않는다."""
    live: dict[tuple[str, str], uuid.UUID] = {}

    class _S(_FakeSession):
        async def execute(self, stmt, params=None):
            if str(stmt) == str(repo._PUBLISH_DRAFT_SQL):
                self.calls.append((str(stmt), params))
                key = (str(_UID), "ko")
                if key in live:  # relationship_profiles_one_published_idx
                    raise IntegrityError("publish", {}, Exception("one_published_idx"))
                live[key] = params["profile_id"]
                return _Res([(params["profile_id"],)])
            return await super().execute(stmt, params)

    async def _publish(pid: uuid.UUID):
        s = _S(draft={**_FakeSession().draft, "id": pid})
        return await repo.publish(s, user_id=_UID, profile_id=pid)

    results = await asyncio.gather(
        _publish(_DRAFT_ID), _publish(uuid.uuid4()), return_exceptions=True
    )
    ok = [r for r in results if isinstance(r, repo.PublishResult)]
    failed = [r for r in results if isinstance(r, IntegrityError)]
    assert len(ok) == 1 and len(failed) == 1 and len(live) == 1


# ─────────────────────────────────────────────────────────────
# publish 순서·원자성
# ─────────────────────────────────────────────────────────────
async def test_publish_locks_context_and_current_published_before_switching():
    s = _FakeSession(published={"id": _PUBLISHED_ID, "version": 1, "render_hash": "old"})
    res = await repo.publish(s, user_id=_UID, profile_id=_DRAFT_ID)
    assert res.status == repo.RESULT_PUBLISHED and res.superseded_id == _PUBLISHED_ID
    order = s.statements
    assert order.index(str(repo._LOCK_CONTEXT_SQL)) < order.index(str(repo._SUPERSEDE_PUBLISHED_SQL))
    assert order.index(str(repo._LOCK_PUBLISHED_SQL)) < order.index(str(repo._SUPERSEDE_PUBLISHED_SQL))
    # 기존을 먼저 닫고 draft를 올린다 — 사이에 commit이 없어야 published가 2개인 순간이 없다.
    assert order.index(str(repo._SUPERSEDE_PUBLISHED_SQL)) < order.index(str(repo._PUBLISH_DRAFT_SQL))
    assert s.committed is False


async def test_first_publish_without_a_previous_row_skips_supersede():
    s = _FakeSession()
    res = await repo.publish(s, user_id=_UID, profile_id=_DRAFT_ID)
    assert res.status == repo.RESULT_PUBLISHED and res.superseded_id is None
    assert not s.ran(repo._SUPERSEDE_PUBLISHED_SQL)


async def test_same_render_hash_does_not_publish_a_new_version():
    """같은 내용 재발행 금지 — 안정 프리픽스가 교체되면 프롬프트 캐시가 통째로 날아간다."""
    s = _FakeSession(published={"id": _PUBLISHED_ID, "version": 1, "render_hash": "draft-hash"})
    res = await repo.publish(s, user_id=_UID, profile_id=_DRAFT_ID)
    assert res.status == repo.RESULT_UNCHANGED and res.version == 1
    assert not s.ran(repo._PUBLISH_DRAFT_SQL) and not s.ran(repo._SUPERSEDE_PUBLISHED_SQL)
    assert s.ran(repo._INVALIDATE_DRAFT_SQL)  # 쓸모없어진 draft는 닫는다


# ─────────────────────────────────────────────────────────────
# stale — 잠근 현재값과 다르면 결과를 폐기한다(재시도하지 않는다).
# ─────────────────────────────────────────────────────────────
async def test_stale_generation_discards_the_result():
    s = _FakeSession(context=(7, 0))
    res = await repo.publish(s, user_id=_UID, profile_id=_DRAFT_ID)
    assert res.status == repo.RESULT_STALE_GENERATION
    assert not s.ran(repo._PUBLISH_DRAFT_SQL) and s.ran(repo._INVALIDATE_DRAFT_SQL)


async def test_stale_input_revision_discards_the_result():
    s = _FakeSession(context=(0, 4))
    res = await repo.publish(s, user_id=_UID, profile_id=_DRAFT_ID)
    assert res.status == repo.RESULT_STALE_PROFILE_INPUT
    assert not s.ran(repo._PUBLISH_DRAFT_SQL)


# ─────────────────────────────────────────────────────────────
# 근거 대조 — JSON ↔ edge 양방향 + 지금도 유효한 근거인지.
# ─────────────────────────────────────────────────────────────
async def test_publish_rejects_a_fact_of_another_user():
    """타 유저 fact는 유저 스코프 쿼리에서 '없는 근거'가 된다 → publish하지 않는다."""
    doc = rp.build_document(
        known_facts=[_item("k", refs=[{"type": "fact", "id": _FACT_OTHER_USER}])]
    )
    s = _FakeSession(
        document=doc,
        draft={**_FakeSession().draft, "document_json": doc.as_json()},
        edges=_edges_of(doc),
        usable_facts={_FACT},  # 그 유저의 active fact 목록에 없다
    )
    with pytest.raises(repo.InvalidProvenanceError):
        await repo.publish(s, user_id=_UID, profile_id=_DRAFT_ID)
    assert not s.ran(repo._PUBLISH_DRAFT_SQL)


async def test_publish_rejects_a_forgotten_or_superseded_source():
    s = _FakeSession(usable_facts=set())  # marker에 걸렸거나 terminal이 됐다
    with pytest.raises(repo.InvalidProvenanceError):
        await repo.publish(s, user_id=_UID, profile_id=_DRAFT_ID)


async def test_publish_rejects_edges_that_disagree_with_the_document():
    s = _FakeSession(edges=[{"item_key": "k", "fact_id": uuid.UUID(_FACT), "insight_id": None}])
    with pytest.raises(repo.InvalidProvenanceError) as e:
        await repo.publish(s, user_id=_UID, profile_id=_DRAFT_ID)
    assert "누락" in str(e.value)  # stance 항목의 ref가 edge에 없다


async def test_publish_rejects_extra_edges_not_in_the_document():
    s = _FakeSession(
        edges=[*_edges_of(_document()), {"item_key": "ghost", "fact_id": uuid.UUID(_FACT), "insight_id": None}]
    )
    with pytest.raises(repo.InvalidProvenanceError):
        await repo.publish(s, user_id=_UID, profile_id=_DRAFT_ID)


async def test_publish_rejects_a_draft_of_another_user():
    s = _FakeSession(draft=None)  # user_id 스코프 조회에서 안 나온다 = 남의 것이거나 없다
    with pytest.raises(repo.InvalidProvenanceError):
        await repo.publish(s, user_id=_OTHER, profile_id=_DRAFT_ID)


async def test_insight_with_a_dead_source_fact_is_not_usable():
    """insight는 자신이 active여도 source fact가 죽으면 근거가 아니다(쿼리가 그렇게 짜여 있다)."""
    sql = "".join(str(repo._USABLE_INSIGHTS_SQL).split())
    assert "FROMmemory_insight_sourcess" in sql and "NOTEXISTS" in sql
    assert "i.user_id=:user_id" in sql and "s.user_id=i.user_id" in sql
    assert "f.user_id=s.user_id" in sql


# ─────────────────────────────────────────────────────────────
# terminal 불가역
# ─────────────────────────────────────────────────────────────
async def test_publishing_an_invalidated_profile_fails_instead_of_reviving_it():
    s = _FakeSession(draft={**_FakeSession().draft, "status": "invalidated"})
    with pytest.raises(repo.TerminalProfileError):
        await repo.publish(s, user_id=_UID, profile_id=_DRAFT_ID)
    assert not s.ran(repo._PUBLISH_DRAFT_SQL)


async def test_publishing_a_superseded_profile_fails():
    s = _FakeSession(draft={**_FakeSession().draft, "status": "superseded"})
    with pytest.raises(repo.TerminalProfileError):
        await repo.publish(s, user_id=_UID, profile_id=_DRAFT_ID)


async def test_publish_update_that_hits_no_row_fails_instead_of_reviving():
    s = _FakeSession(publish_hits=False)  # 그 사이 invalidated로 갔다
    with pytest.raises(repo.TerminalProfileError):
        await repo.publish(s, user_id=_UID, profile_id=_DRAFT_ID)


def _sql(stmt) -> str:
    """공백을 지운 SQL — 서식 변경에 흔들리지 않게(test_memory_repo와 같은 방식)."""
    return "".join(str(stmt).split())


def _update_statements() -> list[tuple[str, str]]:
    """모듈의 `*_SQL` 상수 중 UPDATE 문 전부(새 문장이 추가돼도 자동으로 검사 대상)."""
    out = []
    for name in dir(repo):
        if not name.endswith("_SQL"):
            continue
        s = _sql(getattr(repo, name))
        if s.upper().startswith("UPDATE"):
            out.append((name, s))
    return out


def test_every_status_update_pins_its_starting_state_and_user():
    starts = {
        "_SUPERSEDE_PUBLISHED_SQL": "status='published'",
        "_PUBLISH_DRAFT_SQL": "status='draft'",
        "_INVALIDATE_DRAFT_SQL": "status='draft'",
    }
    names = {n for n, _ in _update_statements()}
    assert names == set(starts)
    for name, s in _update_statements():
        where = s.split("WHERE")[1]
        assert starts[name] in where  # 출발 상태를 못 박는다(0행이면 되살리지 않고 실패)
        assert "user_id=:user_id" in where


def test_no_statement_ever_revives_a_terminal_profile():
    """invalidated/superseded → published(또는 draft)로 되돌리는 경로가 하나도 없어야 한다."""
    for _, s in _update_statements():
        set_clause = s.split("WHERE")[0]
        if "status='published'" in set_clause:
            assert "status='draft'" in s.split("WHERE")[1]  # draft에서만 올라온다
        assert "status='draft'" not in set_clause  # 어떤 상태도 draft로 되돌리지 않는다


def test_repo_never_deletes_profile_rows():
    import inspect

    src = inspect.getsource(repo).upper()
    assert "DELETE FROM" not in src and "TRUNCATE" not in src


def test_every_read_is_scoped_to_the_user():
    for name in dir(repo):
        if not name.endswith("_SQL"):
            continue
        s = _sql(getattr(repo, name))
        assert "user_id=:user_id" in s or "user_id,:user_id" in s or ":user_id," in s, name


# ─────────────────────────────────────────────────────────────
# draft 작성
# ─────────────────────────────────────────────────────────────
async def test_create_draft_writes_edges_that_match_the_document():
    s = _FakeSession()
    res = await repo.create_draft(s, user_id=_UID, language="ko", document=_document())
    assert res.status == repo.STATUS_DRAFT and res.version == 3
    rows = s.executed(repo._INSERT_EDGE_SQL)[0]
    assert {(r["item_key"], str(r["fact_id"])) for r in rows} == {
        ("s", _FACT),
        ("k", _FACT),
    }
    assert all(r["insight_id"] is None for r in rows)
    stored = json.loads(s.executed(repo._INSERT_DRAFT_SQL)[0]["document_json"])
    assert rp.document_from_json(stored).edges() == res.render.document.edges()


async def test_create_draft_skips_the_row_when_nothing_changed():
    doc = _document()
    rendered = rp.render(doc, "ko")
    s = _FakeSession(published={"id": _PUBLISHED_ID, "version": 1, "render_hash": rendered.render_hash})
    res = await repo.create_draft(s, user_id=_UID, language="ko", document=doc)
    assert res.status == repo.RESULT_UNCHANGED and res.profile_id is None
    assert not s.ran(repo._INSERT_DRAFT_SQL) and not s.ran(repo._INSERT_EDGE_SQL)


async def test_create_draft_masks_the_real_name_at_write_time():
    s = _FakeSession()
    doc = rp.build_document(known_facts=[_item("k", "승민은 부산에 산다")])  # 마스킹 없이 들어온 문서
    await repo.create_draft(s, user_id=_UID, language="ko", document=doc, nickname="승민")
    params = s.executed(repo._INSERT_DRAFT_SQL)[0]
    assert "승민" not in params["rendered_text"] and "{유저이름}" in params["rendered_text"]
    assert "승민" not in params["document_json"]


async def test_create_draft_stamps_the_current_input_coordinates():
    s = _FakeSession(context=(3, 11))
    await repo.create_draft(s, user_id=_UID, language="ja", document=_document())
    params = s.executed(repo._INSERT_DRAFT_SQL)[0]
    assert (params["memory_generation"], params["input_revision"]) == (3, 11)
    assert params["locale"] == "ja"


@pytest.mark.parametrize(
    ("language", "locale"), [("ko-KR", "ko"), ("ja-JP", "ja"), ("en-US", "en"), ("zh-Hant-TW", "en")]
)
async def test_draft_locale_comes_from_the_i18n_bucket(language, locale):
    s = _FakeSession()
    await repo.create_draft(s, user_id=_UID, language=language, document=_document())
    assert s.executed(repo._INSERT_DRAFT_SQL)[0]["locale"] == locale


# ─────────────────────────────────────────────────────────────
# 렌더 경로 — 매 턴 근거를 다시 대조한다.
# ─────────────────────────────────────────────────────────────
async def test_prompt_text_drops_items_whose_source_is_no_longer_valid():
    doc = rp.build_document(
        known_facts=[
            _item("keep"),
            _item("gone", "잊어달라고 한 내용", [{"type": "fact", "id": _FACT_OTHER_USER}]),
        ]
    )
    s = _FakeSession(
        published={
            "id": _PUBLISHED_ID,
            "version": 1,
            "document_json": doc.as_json(),
            "rendered_text": "저장된 문자열",
            "render_hash": "h",
        },
        usable_facts={_FACT},
    )
    text = await repo.prompt_text(s, user_id=_UID, language="ko")
    assert "잊어달라고 한 내용" not in text  # 저장된 rendered_text를 그대로 쓰지 않는다
    assert "본문" in text


async def test_prompt_text_is_empty_when_every_source_died():
    doc = _document()
    s = _FakeSession(
        published={
            "id": _PUBLISHED_ID,
            "version": 1,
            "document_json": doc.as_json(),
            "rendered_text": "저장된 문자열",
            "render_hash": "h",
        },
        usable_facts=set(),
    )
    assert await repo.prompt_text(s, user_id=_UID, language="ko") == ""


async def test_prompt_text_is_empty_without_a_published_profile():
    assert await repo.prompt_text(_FakeSession(), user_id=_UID, language="ko") == ""


@pytest.mark.parametrize("language", ["ko", "ja", "en"])
async def test_prompt_text_renders_in_the_users_language(language):
    doc = _document()
    s = _FakeSession(
        published={
            "id": _PUBLISHED_ID,
            "version": 1,
            "document_json": doc.as_json(),
            "rendered_text": "",
            "render_hash": "h",
        }
    )
    text = await repo.prompt_text(s, user_id=_UID, language=language)
    assert f"[{rp.label(rp.SLOT_KNOWN_FACTS, language)}]" in text
