import copy
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.services.affirmation import CATALOG_PATH, daily_affirmation, load_catalog, load_manifest


def catalog_dict():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def test_published_catalog_has_every_locale_for_every_item():
    manifest = load_catalog()
    assert manifest.format_version == 1
    assert len({item.id for item in manifest.items}) == len(manifest.items)
    for item in manifest.items:
        for locale in ("ko", "en", "ja"):
            assert item.text.for_locale(locale).strip()
    assert load_catalog() is load_catalog()  # 재사용 캐시 — 재시작 없이는 파일을 다시 읽지 않는다


def test_selection_is_stable_across_locales_and_restarts_for_the_same_date():
    for offset in range(30):
        day = date(2026, 9, 9) + timedelta(days=offset)
        item = daily_affirmation(day)
        load_catalog.cache_clear()  # 프로세스 재시작과 동등
        assert daily_affirmation(day).id == item.id
        assert {item.text.for_locale(locale) for locale in ("ko", "en", "ja")}
        assert item.text.for_locale("zz") == item.text.en  # 미지원 언어는 영어 폴백


def test_every_item_appears_within_ninety_consecutive_days():
    seen = set()
    for offset in range(90):
        seen.add(daily_affirmation(date(2026, 9, 9) + timedelta(days=offset)).id)
    assert seen == {item.id for item in load_catalog().items}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: raw["items"].append(copy.deepcopy(raw["items"][0])),  # 중복 id
        lambda raw: raw["items"][0]["text"].pop("ja"),  # locale 누락
        lambda raw: raw["items"][0]["text"].update(ko="   "),  # 공백 문구
        lambda raw: raw["items"][0]["text"].update(ko=""),
        lambda raw: raw["items"][0]["text"].update(en="x" * 201),
        lambda raw: raw["items"][0].update(id="Bad Id"),
        lambda raw: raw["items"][0].update(extra=1),
        lambda raw: raw.update(format_version=2),
        lambda raw: raw.update(items=[]),
    ],
)
def test_invalid_catalog_rejected(mutation):
    raw = catalog_dict()
    mutation(raw)
    with pytest.raises(ValueError):
        load_manifest(json.dumps(raw, ensure_ascii=False).encode())


def test_catalog_file_is_bundled_with_the_server_image():
    assert CATALOG_PATH == Path(__file__).resolve().parents[1] / "app/resources/affirmations.json"
    assert CATALOG_PATH.exists()
