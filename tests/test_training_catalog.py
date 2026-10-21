"""트레이닝 원고 검증과 앱에 내려줄 공개 보기."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import training_catalog
from app.services.training_catalog import TrainingCatalog

FIXTURE = Path(__file__).parent / "fixtures/trainings/catalog.json"


def _manifest() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _load(manifest: dict) -> TrainingCatalog:
    return TrainingCatalog.from_bytes(json.dumps(manifest, ensure_ascii=False).encode())


def _stage(manifest: dict, number: int) -> dict:
    return manifest["trainings"][0]["stages"][number - 1]


def test_fixture_loads_in_display_order():
    catalog = TrainingCatalog.load(FIXTURE)
    assert [t.id for t in catalog.trainings] == ["mini", "practice-basics"]
    training = catalog.get("practice-basics")
    assert len(training.stages) == 6
    assert training.stage(3).review_point and training.stage(3).reward_hay == 15
    assert training.stage(7) is None and training.stage(0) is None
    assert catalog.get("missing") is None


def _walk_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _walk_keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _walk_keys(v)}
    return set()


@pytest.mark.parametrize("locale", ["ko", "en", "ja"])
def test_public_stages_hide_answers_hints_explanations_and_criteria(locale):
    training = TrainingCatalog.load(FIXTURE).get("practice-basics")
    views = [training_catalog.public_stage(stage, locale) for stage in training.stages]
    keys = _walk_keys(views)
    assert not keys & {"correct", "hint", "explanation", "grading", "criteria", "example"}
    pick = views[0]
    assert pick["pick_style"] == "choose" and pick["max_length"] is None
    assert [o["id"] for o in pick["options"]] == ["a", "b", "c"]
    write = views[1]
    assert write["pick_style"] is None and write["options"] == [] and write["max_length"] == 200
    assert write["lines"][1] == {
        "speaker": "me", "name": None, "slot": True,
        "text": training.stage(2).lines[1].text.pick(locale),
    }
    assert views[0]["mission"] == training.stage(1).mission.pick(locale)


def _broken(mutate) -> dict:
    manifest = _manifest()
    mutate(manifest)
    return manifest


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda m: _stage(m, 1)["mission"].pop("ja"), id="missing-language"),
        pytest.param(lambda m: _stage(m, 1)["options"][0].update(correct=True, hint=None), id="two-correct"),
        pytest.param(
            lambda m: _stage(m, 1)["options"][1].update(correct=False, hint=_stage(m, 1)["options"][0]["hint"]),
            id="no-correct",
        ),
        pytest.param(lambda m: _stage(m, 4)["options"].pop(), id="one-option"),
        pytest.param(lambda m: _stage(m, 3)["options"].pop(0), id="erase-two-fragments"),
        pytest.param(lambda m: _stage(m, 1)["options"][2].update(id="a"), id="duplicate-option-id"),
        pytest.param(lambda m: _stage(m, 1)["options"][0].pop("hint"), id="wrong-option-without-hint"),
        pytest.param(lambda m: _stage(m, 2).update(number=3), id="numbering-gap"),
        pytest.param(lambda m: _stage(m, 3).update(reward_hay=0), id="review-without-reward"),
        pytest.param(lambda m: _stage(m, 4).update(reward_hay=10), id="reward-without-review"),
        pytest.param(lambda m: _stage(m, 1).update(pick_style="erase"), id="erase-in-chat"),
        pytest.param(lambda m: _stage(m, 2).update(options=_stage(m, 1)["options"]), id="write-with-options"),
        pytest.param(lambda m: _stage(m, 2).pop("grading"), id="write-without-grading"),
        pytest.param(lambda m: _stage(m, 1).update(grading=_stage(m, 2)["grading"]), id="pick-with-grading"),
        pytest.param(lambda m: _stage(m, 1)["lines"][0].update(slot=True), id="two-slots"),
        pytest.param(lambda m: m["trainings"][1]["stages"][0]["lines"][0].pop("name"), id="other-without-name"),
        pytest.param(lambda m: _stage(m, 1)["lines"][0].pop("text"), id="line-without-text"),
        pytest.param(lambda m: _stage(m, 1)["mission"].update(ko=" 공백 "), id="untrimmed"),
        pytest.param(lambda m: _stage(m, 1)["mission"].update(ko="é"), id="not-nfc"),
        pytest.param(lambda m: m["trainings"][1].update(id="practice-basics"), id="duplicate-training"),
        pytest.param(lambda m: _stage(m, 1).update(extra=True), id="unknown-field"),
    ],
)
def test_invalid_manifests_are_rejected(mutate):
    with pytest.raises(ValueError):
        _load(_broken(mutate))


def test_duplicate_json_keys_and_non_finite_numbers_are_rejected():
    raw = FIXTURE.read_bytes()
    with pytest.raises(ValueError):
        TrainingCatalog.from_bytes(raw.replace(b'"schema_version": 1,', b'"schema_version": 1, "schema_version": 1,'))
    with pytest.raises(ValueError):
        TrainingCatalog.from_bytes(raw.replace(b'"order": 2', b'"order": NaN'))


def test_current_is_empty_without_a_catalog_and_when_it_is_broken(monkeypatch, caplog):
    def missing(cls, path=None):
        raise FileNotFoundError

    def broken(cls, path=None):
        raise ValueError("bad")

    training_catalog.current.cache_clear()
    try:
        monkeypatch.setattr(TrainingCatalog, "load", classmethod(missing))
        assert training_catalog.current() is training_catalog.EMPTY
        training_catalog.current.cache_clear()
        monkeypatch.setattr(TrainingCatalog, "load", classmethod(broken))
        assert training_catalog.current() is training_catalog.EMPTY
        assert "training catalog unavailable: ValueError" in caplog.text
    finally:
        training_catalog.current.cache_clear()



# '자책 금지' 뼈대(요구사항 '가정'의 단계표). 공개 뒤 보기 id·정답 id를 바꾸면 저장된 답이 어긋난다.
# 단계: (답 방식, 모양, 지우기, 보기 id, 정답 id, 풀이, 보상)
NO_SELF_BLAME = {
    1: ("pick", "chat", False, "abc", "b", False, 0),
    2: ("pick", "chat", False, "ab", "a", False, 0),
    3: ("write", "chat", False, "", None, False, 0),
    4: ("pick", "chat", False, "abc", "b", True, 0),
    5: ("pick", "diary", True, "abc", "c", False, 15),
    6: ("pick", "chat", False, "ab", "b", False, 0),
    7: ("pick", "chat", False, "abc", "b", True, 0),
    8: ("pick", "chat", False, "ab", "a", False, 0),
    9: ("write", "chat", False, "", None, False, 0),
    10: ("pick", "chat", False, "ab", "a", False, 0),
    11: ("pick", "diary", True, "abcd", "b", False, 0),
    12: ("pick", "chat", False, "ab", "a", False, 0),
    13: ("pick", "chat", False, "ab", "b", False, 0),
    14: ("pick", "chat", False, "abc", "b", True, 0),
    15: ("write", "chat", False, "", None, False, 40),
    16: ("pick", "chat", False, "ab", "b", False, 0),
    17: ("pick", "diary", True, "abcd", "d", False, 0),
    18: ("pick", "chat", False, "ab", "a", False, 0),
    19: ("pick", "diary", False, "ab", "b", False, 0),
    20: ("write", "diary", False, "", None, False, 0),
    21: ("pick", "diary", False, "ab", "a", True, 0),
    22: ("pick", "diary", True, "abc", "b", False, 0),
    23: ("pick", "chat", False, "ab", "b", False, 0),
    24: ("pick", "chat", False, "ab", "a", False, 0),
    25: ("write", "chat", False, "", None, False, 0),
    26: ("pick", "diary", False, "ab", "b", True, 0),
    27: ("write", "diary", False, "", None, False, 0),
    28: ("write", "diary", False, "", None, False, 0),
    29: ("write", "diary", False, "", None, False, 0),
    30: ("write", "diary", False, "", None, False, 100),
}


def test_shipped_catalog_keeps_the_no_self_blame_skeleton():
    training = TrainingCatalog.load().get("no-self-blame")
    assert training is not None and len(training.stages) == len(NO_SELF_BLAME)
    for stage in training.stages:
        correct = next((o.id for o in stage.options if o.correct), None)
        actual = (
            stage.answer_type, stage.layout, stage.pick_style == "erase",
            "".join(o.id for o in stage.options), correct, stage.explanation is not None, stage.reward_hay,
        )
        assert actual == NO_SELF_BLAME[stage.number], stage.number
