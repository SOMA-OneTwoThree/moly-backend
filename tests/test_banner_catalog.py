import copy
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.services.banner_catalog import (
    BannerCatalog,
    binding_values,
    capabilities,
    compile_canvas,
    render_feed,
    select_candidates,
)


def manifest():
    wire = json.loads((Path(__file__).parent / "fixtures/banners/wire_feed.json").read_text())
    canvas = wire["items"][0]["canvas"]
    for element in canvas["elements"]:
        element["text"] = {"kind": "template", "value": element["text"]}
    canvas["elements"][0]["text"]["value"] = "{today}"
    canvas["elements"][1]["text"]["value"] = "{remaining} routines"
    return {
        "manifest_format_version": 1,
        "wire_schema_version": 1,
        "placement": "home_blind",
        "enabled": True,
        "banners": [
            {
                "id": "routines",
                "component": "banner_canvas_v1",
                "layout_profile": "home_blind_v1",
                "enabled": True,
                "starts_at": None,
                "ends_at": None,
                "platforms": ["ios", "android"],
                "min_app_version": None,
                "max_app_version_exclusive": None,
                "bindings": {
                    "today": {"source": "user.local_date", "format": "month_day"},
                    "remaining": {"source": "routines.remaining_today", "format": None},
                },
                "when": {"binding": "remaining", "operator": "gt", "value": 0},
                "canvases_by_locale": {"en": canvas},
            }
        ],
    }


def load(raw=None):
    return BannerCatalog.from_bytes(json.dumps(raw or manifest(), ensure_ascii=False).encode())


def test_exact_bytes_revision_and_frozen_snapshot():
    raw = json.dumps(manifest()).encode()
    catalog = BannerCatalog.from_bytes(raw)
    assert catalog.revision == hashlib.sha256(raw).hexdigest()
    assert BannerCatalog.from_bytes(raw + b"\n").revision != catalog.revision
    with pytest.raises(TypeError):
        catalog.manifest.banners[0].bindings["evil"] = None


@pytest.mark.parametrize(
    "raw",
    [
        b'{"enabled":true,"enabled":false}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b"x" * (256 * 1024 + 1),
        b"\xff",
    ],
)
def test_bad_bytes_rejected(raw):
    with pytest.raises(ValueError):
        BannerCatalog.from_bytes(raw)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: b["canvases_by_locale"]["en"].update(width=400),
        lambda b: b["canvases_by_locale"]["en"]["elements"][0]["frame"].update(x=0.9),
        lambda b: b["canvases_by_locale"]["en"]["elements"][0]["text"].update(value="{today.year}"),
        lambda b: b["canvases_by_locale"]["en"]["elements"][0]["text"].update(value="{unknown}"),
        lambda b: b["canvases_by_locale"]["en"]["elements"][0]["text"].update(value="x" * 121),
        lambda b: b.update(when=None),
        lambda b: b.update(starts_at="2026-01-01T00:00:00"),
        lambda b: b.update(min_app_version="2.0.0", max_app_version_exclusive="1.0.0"),
    ],
)
def test_invalid_authoring_rejected(mutation):
    raw = manifest()
    mutation(raw["banners"][0])
    with pytest.raises(ValueError):
        load(raw)


def test_compilation_selection_zero_and_missing_data():
    catalog = load()
    banner = catalog.manifest.banners[0]
    canvas = banner.canvases_by_locale["en"]
    now = datetime(2026, 9, 5, 3, tzinfo=timezone.utc)
    deadline = datetime(2026, 9, 5, 15, tzinfo=timezone.utc)
    params = dict(
        now=now, platform="ios", app_version="1.2.3", locale="ja", supported=capabilities(canvas)
    )
    selected = select_candidates(catalog, **params)
    assert selected[0][1] == "en"
    assert not select_candidates(catalog, **(params | {"supported": frozenset()}))
    for remaining, expected in ((3, 1), (0, 0), (None, 0)):
        feed = render_feed(
            catalog,
            selected,
            now=now,
            local_date=date(2026, 9, 5),
            day_ends_at=deadline,
            remaining=remaining,
        )
        assert len(feed.items) == expected
        if expected:
            assert feed.items[0].canvas.elements[1].text == "3 routines"
            assert feed.items[0].valid_until == deadline
    assert canvas.elements[1].text.value == "{remaining} routines"


def test_count_cases_and_literal_braces():
    raw = manifest()
    text = raw["banners"][0]["canvases_by_locale"]["en"]["elements"][1]
    text["text"] = {
        "kind": "count_cases",
        "binding": "remaining",
        **{
            key: {"kind": "template", "value": "{{" + key + "}} {remaining}"}
            for key in ("zero", "one", "other")
        },
    }
    banner = load(raw).manifest.banners[0]
    for count, key in ((0, "zero"), (1, "one"), (2, "other")):
        canvas = compile_canvas(
            banner.canvases_by_locale["en"], binding_values(banner, "en", date(2026, 9, 5), count)
        )
        assert canvas.elements[1].text == "{" + key + "} " + str(count)


def test_bad_runtime_card_does_not_remove_static_card():
    raw = manifest()
    static = copy.deepcopy(raw["banners"][0])
    static.update(id="static", bindings={}, when=None)
    for element in static["canvases_by_locale"]["en"]["elements"]:
        element["text"] = {"kind": "template", "value": "Hello"}
    raw["banners"].append(static)
    catalog = load(raw)
    now = datetime(2026, 9, 5, tzinfo=timezone.utc)
    selected = select_candidates(
        catalog,
        now=now,
        platform="ios",
        app_version="dev",
        locale="en",
        supported=capabilities(catalog.manifest.banners[0].canvases_by_locale["en"]),
    )
    feed = render_feed(
        catalog, selected, now=now, local_date=None, day_ends_at=None, remaining=None
    )
    assert [c.id for c in feed.items] == ["static"]


def composed_action_manifest():
    raw = manifest()
    canvas = raw["banners"][0]["canvases_by_locale"]["en"]
    label = canvas["elements"][0]
    label["frame"] = {"x": 0.12, "y": 0.56, "width": 0.4, "height": 0.2}
    canvas["elements"] = [
        {
            "id": "surface",
            "type": "shape_v1",
            "frame": {"x": 0.1, "y": 0.5, "width": 0.5, "height": 0.35},
            "background": {"type": "solid_v1", "color": "#FFFFFF"},
            "radius": 12,
            "border": {"width": 0, "color": "#FFFFFF"},
            "semantics_order": None,
        },
        label,
        {
            "id": "shop-action",
            "type": "action_region_v1",
            "frame": {"x": 0.1, "y": 0.5, "width": 0.5, "height": 0.35},
            "content_ids": ["surface", label["id"]],
            "accessibility_label": "Visit shop",
            "semantics_order": 2,
            "action": {"type": "open_shop"},
        },
    ]
    return raw


def test_composed_action_compiles_and_requires_new_capabilities():
    catalog = load(composed_action_manifest())
    banner = catalog.manifest.banners[0]
    canvas = banner.canvases_by_locale["en"]
    wire = compile_canvas(canvas, {"today": "Sep 6", "remaining": 1})
    assert wire.elements[1].text == "Sep 6"
    assert wire.elements[2].action.type == "open_shop"
    supported = capabilities(canvas)
    assert {"shape_v1", "action_region_v1", "open_shop"} <= supported
    params = dict(now=datetime.now(timezone.utc), platform="ios", app_version="1.1.6", locale="en")
    assert select_candidates(catalog, supported=supported, **params)
    assert not select_candidates(catalog, supported=supported - {"action_region_v1"}, **params)


@pytest.mark.parametrize(
    "references", [["missing"], ["shop-action"], ["surface"], ["surface", "surface"]]
)
def test_composed_action_rejects_invalid_references(references):
    raw = composed_action_manifest()
    raw["banners"][0]["canvases_by_locale"]["en"]["elements"][-1]["content_ids"] = references
    with pytest.raises(ValueError):
        load(raw)


def test_composed_action_rejects_content_outside_region():
    raw = composed_action_manifest()
    raw["banners"][0]["canvases_by_locale"]["en"]["elements"][1]["frame"]["x"] = 0.01
    with pytest.raises(ValueError, match="outside"):
        load(raw)


def test_composed_action_rejects_shared_visual_ownership():
    raw = composed_action_manifest()
    elements = raw["banners"][0]["canvases_by_locale"]["en"]["elements"]
    other = copy.deepcopy(elements[-1])
    other.update(id="second-action", semantics_order=3)
    elements.append(other)
    with pytest.raises(ValueError, match="multiple actions"):
        load(raw)
