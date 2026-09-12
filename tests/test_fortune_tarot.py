"""Independent card selection, whole-paragraph provenance and rollback contracts."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import re
import shutil
from types import SimpleNamespace
import uuid

import pytest

from app.services import fortune, fortune_catalog, fortune_copy_selection as selection, fortune_scores, fortune_tarot

RESOURCES = Path(__file__).resolve().parents[1] / "app/resources/fortune"
UID = "00000000-0000-0000-0000-000000000001"
DAY = date(2026, 9, 10)
BIRTH = date(2002, 12, 13)


def _save(target: Path, filename: str, value) -> None:
    path = target / filename
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    manifest_path = target / "manifest.v2.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["assets"][filename]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


def _build(
    *, birth=BIRTH, day=DAY, previous=None, copies=None, policy="tarot",
    user_id=UID, timezone_name="Asia/Seoul", first_visit=False, experience_enabled=True,
):
    return fortune._build_result(
        profile=SimpleNamespace(user_id=uuid.UUID(user_id), birth_date=birth), today=day,
        timezone_name=timezone_name, previous_semantic=previous, previous_copies=copies,
        copy_policy=policy, first_visit=first_visit, experience_enabled=experience_enabled,
    )


def test_exact_780_readings_bind_complete_copy_in_three_locales():
    catalog = fortune_catalog.load_catalog()
    asset = json.loads((RESOURCES / fortune_tarot.FILENAME).read_text())
    assert len(asset["readings"]) == 780
    assert set(asset["readings"]) == fortune_tarot.READING_KEYS
    assert fortune_tarot.FILENAME in catalog.asset_hashes
    assert len(fortune_tarot.CARD_IDS) == 78
    assert all(set(e["copy_sha256"]) == {"ko", "en", "ja"} for e in asset["readings"].values())


@pytest.mark.parametrize("mutation,message", [
    ("missing", "coverage"), ("card", "identity"), ("orientation", "identity"),
    ("observations", "observations"), ("binding", "hash mismatch"), ("meaning", "editorial text"),
])
def test_invalid_editorial_rejected_even_with_valid_manifest(tmp_path, mutation, message):
    target = tmp_path / "fortune"
    shutil.copytree(RESOURCES, target)
    asset = json.loads((target / fortune_tarot.FILENAME).read_text())
    key = "work.major.sun.upright"
    entry = asset["readings"][key]
    if mutation == "missing":
        asset["readings"].pop(key)
    elif mutation == "card":
        entry["card_id"] = "major.invented"
    elif mutation == "orientation":
        entry["orientation"] = "automatic"
    elif mutation == "observations":
        entry["observations"] = ["Only one observation"]
    elif mutation == "meaning":
        entry["meaning"] = ""
    else:
        entry["copy_sha256"]["ja"] = "0" * 64
    _save(target, fortune_tarot.FILENAME, asset)
    with pytest.raises(fortune_catalog.FortuneCatalogError, match=message):
        fortune_catalog.load_catalog(target)


def test_locale_edit_cannot_silently_keep_stale_card_brief(tmp_path):
    target = tmp_path / "fortune"
    shutil.copytree(RESOURCES, target)
    asset = json.loads((target / "copy.v3.en.json").read_text())
    asset["readings"]["work.major.sun.upright"]["text"][0] = "A changed reading stays here. This is the second sentence."
    _save(target, "copy.v3.en.json", asset)
    with pytest.raises(fortune_catalog.FortuneCatalogError, match="editorial/copy hash mismatch"):
        fortune_catalog.load_catalog(target)


@pytest.mark.parametrize("orientation", ("upright", "reversed"))
@pytest.mark.parametrize("index", range(78))
def test_every_card_and_axis_renders_without_internal_metadata(index, orientation):
    deck = sorted(fortune_tarot.CARD_IDS)
    state = {"version": selection.CARD_SELECTION_VERSION, "day": DAY.isoformat(), "cards": {
        axis: {"card_id": deck[(index + offset) % 78], "orientation": orientation}
        for offset, axis in enumerate(fortune_tarot.AXES)
    }}
    semantic = fortune_scores.generate_result(birth_date=date(2002, 12, 13), local_date=DAY)
    rendered = fortune_catalog.render_cards_all(semantic, selection=state)
    for localized in rendered.values():
        assert set(localized) == {"overall", "categories", "lucky_color"}
        assert set(localized["overall"]) == {"headline", "flow", "do", "pause"}
        assert all(set(item) == {"text"} for item in localized["categories"].values())
        payload = json.dumps(localized, ensure_ascii=False)
        assert not re.search(r"타로|タロット|tarot|major\.[a-z_]+|(?:cups|swords|wands|pentacles)\.\d", payload, re.I)
        assert not any(token in payload for token in ('"card_id"', '"orientation"', '"observations"', '"meaning"'))


@pytest.mark.parametrize("first_visit", (False, True))
def test_birth_date_mode_draw_is_identical_across_users_timezones_and_locales(first_visit):
    first, copies = _build(first_visit=first_visit)
    second, other_copies = _build(
        user_id="abcdefab-cdef-abcd-efab-cdefabcdefab", timezone_name="America/New_York",
        first_visit=first_visit,
    )
    # The same local date, birth and mode share the entire result, not just a score band.
    assert second == first
    assert other_copies == copies
    assert set(copies) == set(fortune_catalog.SUPPORTED_LOCALES)
    assert first["draw_algorithm_version"] == selection.DRAW_ALGORITHM_VERSION
    expected = selection.draw_cards(birth_date=BIRTH, today=DAY, first_visit=first_visit)
    assert first["copy_selection"] == expected
    assert selection.draw_cards(
        birth_date=BIRTH, today=DAY, first_visit=first_visit,
        user_id="abcdefab-cdef-abcd-efab-cdefabcdefab",
    ) == expected


def test_new_draw_uses_birth_and_mode_without_score_inputs():
    regular, _ = _build()
    other_birth, _ = _build(birth=date(1980, 3, 14))
    intro, _ = _build(first_visit=True)
    assert regular["copy_selection"] != other_birth["copy_selection"]
    assert other_birth["copy_selection"] == selection.draw_cards(
        birth_date=date(1980, 3, 14), today=DAY, first_visit=False,
    )
    assert intro["copy_selection"] == fortune_catalog.first_visit_selection(birth_date=BIRTH, today=DAY)
    assert intro["copy_selection"] != regular["copy_selection"]


def test_legacy_uuid_draw_remains_independent_of_birth_scores_and_locale():
    first, copies = _build(experience_enabled=False)
    second, other_copies = _build(birth=date(1980, 3, 14), experience_enabled=False)
    assert first["copy_selection"] == second["copy_selection"]
    assert "draw_algorithm_version" not in first
    assert first["overall"]["score"] != second["overall"]["score"] or first["categories"] != second["categories"]
    for locale in fortune_catalog.SUPPORTED_LOCALES:
        assert copies[locale]["overall"] == other_copies[locale]["overall"]
        assert copies[locale]["categories"] == other_copies[locale]["categories"]
    legacy_uid = "abcdefab-cdef-abcd-efab-cdefabcdefab"
    assert selection.draw_cards(user_id=legacy_uid, today=DAY) == selection.draw_cards(
        user_id=legacy_uid.upper(), today=DAY,
    )
    assert first["copy_selection"] == selection.draw_cards(user_id=UID, today=DAY)


def test_profile_edit_preserves_stored_prose_even_after_catalog_changes(monkeypatch):
    first, copies = _build()
    saved = deepcopy(copies)
    saved["ko"]["overall"]["headline"] = "저장된 제목은 그대로 보여줘."
    def no_render(*args, **kwargs):
        pytest.fail("same-day profile edit must not reread card prose")
    monkeypatch.setattr(fortune_catalog, "render_cards_all", no_render)
    second, updated = _build(birth=date(1990, 1, 2), previous=first, copies=saved)
    assert second["copy_selection"] == first["copy_selection"]
    for locale in fortune_catalog.SUPPORTED_LOCALES:
        assert updated[locale]["overall"] == saved[locale]["overall"]
        assert updated[locale]["categories"] == saved[locale]["categories"]
        assert updated[locale]["lucky_color"]["key"] == second["lucky_color_key"]
    assert copies["ko"]["overall"] != saved["ko"]["overall"]


def test_missing_same_day_snapshot_and_unknown_selection_fail_closed():
    first, _ = _build()
    with pytest.raises(fortune_catalog.FortuneCatalogError, match="stored copy snapshot"):
        _build(previous=first)
    first["copy_selection"]["version"] = "fortune-selection.v999"
    with pytest.raises(selection.FortuneSelectionError):
        _build(previous=first, day=DAY + timedelta(days=1))


@pytest.mark.parametrize("mutation", ("duplicate", "orientation", "day", "extra", "card"))
def test_invalid_card_draws_fail_closed(mutation):
    state = selection.draw_cards(user_id=UID, today=DAY)
    if mutation == "duplicate":
        state["cards"]["love"] = state["cards"]["overall"]
    elif mutation == "orientation":
        state["cards"]["love"]["orientation"] = "random"
    elif mutation == "day":
        state["day"] = "20260910"
    elif mutation == "extra":
        state["score"] = 80
    else:
        state["cards"]["love"]["card_id"] = "invented"
    with pytest.raises(selection.FortuneSelectionError):
        selection.validate_card_selection(state)


def test_rollback_policy_reads_saved_cards_then_generates_legacy_on_next_date():
    first, copies = _build()
    same, same_copies = _build(previous=first, copies=copies, policy="legacy")
    assert (same, same_copies) == (first, copies)
    tomorrow, legacy_copies = _build(previous=first, copies=copies, policy="legacy", day=DAY + timedelta(days=1))
    assert tomorrow["copy_selection"]["version"] == "fortune-selection.v3"
    assert set(legacy_copies) == {"ko", "en", "ja"}
    restored, _ = _build(previous=tomorrow, day=DAY + timedelta(days=2))
    assert restored["copy_selection"] == selection.draw_cards(
        birth_date=BIRTH, today=DAY + timedelta(days=2), first_visit=False,
    )


def test_new_date_and_deleted_profile_recreation_follow_deterministic_date_policy():
    first, copies = _build()
    second, _ = _build(previous=first, copies=copies, day=DAY + timedelta(days=1))
    assert second["copy_selection"] == selection.draw_cards(
        birth_date=BIRTH, today=DAY + timedelta(days=1), first_visit=False,
    )
    recreated, _ = _build()
    assert recreated["copy_selection"] == first["copy_selection"]


def test_card_draw_accepts_birth_date_and_mode_but_no_score_or_locale():
    import inspect
    parameters = inspect.signature(selection.draw_cards).parameters
    assert set(parameters) == {"user_id", "today", "birth_date", "first_visit"}
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters.values())
    assert parameters["user_id"].default is None  # UUID is optional legacy compatibility only.
    assert parameters["first_visit"].default is False


def test_legacy_uuid_draw_distribution_has_no_missing_cards_or_persistent_orientation_bias():
    from collections import Counter
    cards = Counter()
    upright = Counter()
    for index in range(4000):
        state = selection.draw_cards(user_id=str(uuid.UUID(int=index + 1)), today=DAY)
        assert len({card["card_id"] for card in state["cards"].values()}) == 5
        for axis, card in state["cards"].items():
            cards[axis, card["card_id"]] += 1
            upright[axis] += card["orientation"] == "upright"
    for axis in fortune_tarot.AXES:
        assert 1700 <= upright[axis] <= 2300
        assert all(20 <= cards[axis, card] <= 90 for card in fortune_tarot.CARD_IDS)


@pytest.mark.parametrize("invalid", (None, {}, {"version": "fortune-selection.v999"}))
def test_invalid_prior_selection_never_resets_into_new_card_draw(invalid):
    previous = fortune_scores.generate_result(birth_date=date(2002, 12, 13), local_date=DAY)
    previous["copy_selection"] = invalid
    with pytest.raises(selection.FortuneSelectionError):
        _build(previous=previous, day=DAY + timedelta(days=1))
