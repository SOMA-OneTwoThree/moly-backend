import copy
import json
from datetime import datetime

import pytest

from app.services.topic_catalog import (
    MAX_BYTES,
    TopicCatalog,
    TopicQuestions,
    content_revision,
    entry_expiration,
)


def manifest(count=3):
    topics, sequence = [], []
    for i in range(count):
        questions = TopicQuestions(ko=f"주제 {i}?", en=f"Topic {i}?", ja=f"話題 {i}？")
        revision = content_revision(questions)
        topic_id = f"topic-{i}"
        topics.append({
            "id": topic_id, "category": "test",
            "versions": [{"revision": revision, "questions": questions.model_dump()}],
        })
        sequence.append({"topic_id": topic_id, "topic_revision": revision})
    return {"schema_version": 1, "enabled": True, "topics": topics,
            "sequence": sequence, "revoked": []}


def load(raw):
    return TopicCatalog.from_bytes(json.dumps(raw, ensure_ascii=False).encode())


def test_all_ninety_topics_precede_repeat():
    catalog = load(manifest(90))
    current = None
    seen = []
    for _ in range(91):
        pointer = catalog.next_pointer(current)
        seen.append(pointer.topic_id)
        current = pointer.topic_id
    assert len(set(seen[:90])) == 90
    assert seen[90] == seen[0]


def test_revocation_skips_content_and_exhaustion_does_not_repeat():
    raw = manifest()
    raw["revoked"] = [raw["sequence"][1]]
    catalog = load(raw)
    assert catalog.next_pointer("topic-0").topic_id == "topic-2"
    raw["revoked"].append(raw["sequence"][2])
    assert load(raw).next_pointer("topic-0") is None


def test_rollback_cannot_reinterpret_unknown_cursor():
    assert load(manifest()).next_pointer("newer-topic") is None


def test_emergency_revocation_of_all_topics_is_a_valid_empty_selection():
    raw = manifest()
    raw["revoked"] = copy.deepcopy(raw["sequence"])
    assert load(raw).next_pointer(None) is None


def test_append_preserves_existing_successor():
    old, new = load(manifest(3)), load(manifest(4))
    new.validate_update(old)
    assert new.next_pointer("topic-1") == old.next_pointer("topic-1")
    assert new.next_pointer("topic-2").topic_id == "topic-3"


@pytest.mark.parametrize("mutation", [
    lambda raw: raw["sequence"].reverse(),
    lambda raw: raw["sequence"].pop(),
    lambda raw: raw["topics"].pop(),
])
def test_published_sequence_and_content_cannot_be_removed_or_reordered(mutation):
    raw = manifest()
    old = load(raw)
    mutation(raw)
    with pytest.raises(ValueError):
        load(raw).validate_update(old)


def test_revocations_survive_rollback_and_unknown_old_revision():
    raw = manifest()
    raw["revoked"] = [{"topic_id": "old-topic", "topic_revision": "a" * 64}]
    previous = load(raw)
    with pytest.raises(ValueError, match="revocations"):
        load(manifest()).validate_update(previous)


def test_new_revision_preserves_old_question_snapshot():
    raw = manifest()
    old = load(raw)
    questions = TopicQuestions(ko="다른 질문?", en="Another question?", ja="別の質問？")
    revision = content_revision(questions)
    raw["topics"][0]["versions"].append(
        {"revision": revision, "questions": questions.model_dump()}
    )
    raw["sequence"][0]["topic_revision"] = revision
    new = load(raw)
    new.validate_update(old)
    assert new.questions(new.next_pointer(None)).ko == "다른 질문?"
    assert old.questions(old.next_pointer(None)).ko == "주제 0?"


@pytest.mark.parametrize("mutation", [
    lambda r: r["topics"].append(copy.deepcopy(r["topics"][0])),
    lambda r: r["sequence"].append(copy.deepcopy(r["sequence"][0])),
    lambda r: r["sequence"][0].update(topic_revision="a" * 64),
    lambda r: r["topics"][0]["versions"][0]["questions"].pop("ja"),
    lambda r: r["topics"][0]["versions"][0]["questions"].update(ko="수정했지만 hash 유지?"),
    lambda r: r.update(sequence=[]),
])
def test_invalid_catalog_rejected(mutation):
    raw = manifest()
    mutation(raw)
    with pytest.raises(ValueError):
        load(raw)


@pytest.mark.parametrize("raw", [b'{"enabled":true,"enabled":false}', b'{"x":NaN}',
                                 b"\xff", b"x" * (MAX_BYTES + 1)])
def test_bad_bytes_rejected(raw):
    with pytest.raises(ValueError):
        TopicCatalog.from_bytes(raw)


@pytest.mark.parametrize("question", ["", " 질문?", "질문?\n", "a" * 121, "{유저이름}아?"])
def test_question_bounds_and_literal_text(question):
    with pytest.raises(ValueError):
        TopicQuestions(ko=question, en="Question?", ja="質問？")


@pytest.mark.parametrize("now,zone,expected", [
    ("2026-09-07T14:59:00+00:00", "Asia/Seoul", "2026-09-07T15:29:00+00:00"),
    ("2026-09-07T01:00:00+00:00", "Asia/Seoul", "2026-09-07T15:00:00+00:00"),
    ("2026-03-08T08:00:00+00:00", "America/Los_Angeles", "2026-03-09T07:00:00+00:00"),
    ("2026-11-01T07:00:00+00:00", "America/Los_Angeles", "2026-11-02T08:00:00+00:00"),
])
def test_calendar_midnight_and_minimum_grace(now, zone, expected):
    assert entry_expiration(datetime.fromisoformat(now), zone) == datetime.fromisoformat(expected)


def test_catalog_has_forty_complete_languages_and_is_immutable():
    catalog = TopicCatalog.load()
    assert sum(not catalog.is_revoked(p.topic_id, p.topic_revision)
               for p in catalog.manifest.sequence) == 40
    for pointer in catalog.manifest.sequence:
        questions = catalog.questions(pointer)
        assert questions.for_locale("fr") == questions.en
        assert all(questions.for_locale(locale) for locale in ("ko", "en", "ja"))
    with pytest.raises(TypeError):
        catalog.positions["evil"] = 1
    with pytest.raises(ValueError):
        catalog.manifest.enabled = False
