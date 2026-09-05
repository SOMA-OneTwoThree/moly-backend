"""Banner v1 authoring and wire models. Runtime geometry remains client-owned."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from string import Formatter
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BANNER_WIDTH = 287.7
BANNER_HEIGHT = 158.457
ASSET_ORIGINS = frozenset({"https://qkgjlgzsharnilxnkytd.supabase.co"})
Id = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
Color = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]
Scalar = Annotated[float, Field(allow_inf_nan=False)]
Unit = Annotated[Scalar, Field(ge=0, le=1)]


class BannerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @field_validator(
        "manifest_format_version",
        "wire_schema_version",
        "schema_version",
        "weight",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def numeric_literals_are_not_booleans(cls, value):
        if isinstance(value, bool):
            raise ValueError("numeric identifiers cannot be booleans")
        return value


class BannerFrame(BannerModel):
    x: Unit
    y: Unit
    width: Annotated[Scalar, Field(gt=0, le=1)]
    height: Annotated[Scalar, Field(gt=0, le=1)]

    @model_validator(mode="after")
    def contained(self):
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("frame exceeds fixed canvas")
        return self


class BannerBorder(BannerModel):
    width: Annotated[Scalar, Field(ge=0, le=3)]
    color: Color


class BannerAlignment(BannerModel):
    x: Unit
    y: Unit


class BannerImageSource(BannerModel):
    url: Annotated[str, Field(max_length=2048)]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    byte_length: Annotated[int, Field(gt=0, le=512 * 1024)]
    pixel_width: Annotated[int, Field(gt=0, le=2048)]
    pixel_height: Annotated[int, Field(gt=0, le=2048)]
    media_type: Literal["image/png", "image/jpeg", "image/webp"]

    @model_validator(mode="after")
    def safe_source(self):
        parsed = urlsplit(self.url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if (
            origin not in ASSET_ORIGINS
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/storage/v1/object/public/")
        ):
            raise ValueError("image must use an immutable public asset URL")
        if any(ord(c) < 33 for c in self.url):
            raise ValueError("invalid URL characters")
        if self.pixel_width * self.pixel_height > 1_048_576:
            raise ValueError("image pixel budget exceeded")
        return self


class BannerSolid(BannerModel):
    type: Literal["solid_v1"]
    color: Color


class BannerGradient(BannerModel):
    type: Literal["linear_gradient_v1"]
    colors: Annotated[tuple[Color, Color], Field(min_length=2, max_length=2)]
    direction: Literal["horizontal", "vertical", "diagonal_down", "diagonal_up"]


class BannerImageBackground(BannerModel):
    type: Literal["image_background_v1"]
    source: BannerImageSource
    fit: Literal["cover"]
    alignment: BannerAlignment
    base_color: Color


Background = Annotated[
    BannerSolid | BannerGradient | BannerImageBackground, Field(discriminator="type")
]


class BannerStyle(BannerModel):
    font: Literal["body", "display"]
    font_size: Annotated[Scalar, Field(ge=12, le=28)]
    weight: Literal[400, 500, 600, 700]
    color: Color
    align: Literal["start", "center", "end"]
    max_lines: Annotated[int, Field(ge=1, le=3)]
    line_height: Annotated[Scalar, Field(ge=1, le=2)]


class BannerAction(BannerModel):
    type: Literal["open_shop", "open_routines", "open_conversation", "open_fortune"]


def template_aliases(value: str) -> set[str]:
    remainder = re.sub(r"{{|}}|{[a-z][a-z0-9_]{0,63}}", "", value)
    if "{" in remainder or "}" in remainder:
        raise ValueError("only plain binding aliases and escaped braces are allowed")
    aliases = set()
    for _, name, spec, conversion in Formatter().parse(value):
        if name is not None:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or spec or conversion:
                raise ValueError("only plain binding aliases are allowed")
            aliases.add(name)
    return aliases


class BannerTemplate(BannerModel):
    kind: Literal["template"]
    value: Annotated[str, Field(min_length=1, max_length=1024)]

    @model_validator(mode="after")
    def valid_template(self):
        template_aliases(self.value)
        return self


class BannerCountCases(BannerModel):
    kind: Literal["count_cases"]
    binding: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    zero: BannerTemplate
    one: BannerTemplate
    other: BannerTemplate


TextExpression = Annotated[BannerTemplate | BannerCountCases, Field(discriminator="kind")]


class BannerText(BannerModel):
    id: Id
    type: Literal["text_v1"]
    semantics_order: Annotated[int, Field(ge=0, le=11)]
    vertical_align: Literal["top", "center", "bottom"]
    frame: BannerFrame
    text: Annotated[str, Field(min_length=1, max_length=120)]
    style: BannerStyle


class BannerButton(BannerText):
    type: Literal["button_v1"]
    text: Annotated[str, Field(min_length=1, max_length=20)]
    background_color: Color
    radius: Annotated[Scalar, Field(ge=0, le=24)]
    border: BannerBorder
    padding_horizontal: Annotated[Scalar, Field(ge=0, le=24)]
    padding_vertical: Annotated[Scalar, Field(ge=0, le=12)]
    action: BannerAction

    @model_validator(mode="after")
    def button_geometry(self):
        if isinstance(self.text, str) and ("\n" in self.text or "\r" in self.text):
            raise ValueError("button text must be one line")
        if self.style.max_lines != 1:
            raise ValueError("button max_lines must be one")
        width, height = self.frame.width * BANNER_WIDTH, self.frame.height * BANNER_HEIGHT
        if self.radius > min(width, height) / 2:
            raise ValueError("radius exceeds half short side")
        if self.padding_horizontal * 2 >= width or self.padding_vertical * 2 >= height:
            raise ValueError("button padding leaves no content area")
        return self


class BannerImage(BannerModel):
    id: Id
    type: Literal["image_v1"]
    frame: BannerFrame
    source: BannerImageSource
    fit: Literal["contain", "cover"]
    alignment: BannerAlignment
    accessibility_label: Annotated[str, Field(min_length=1, max_length=120)] | None
    semantics_order: Annotated[int, Field(ge=0, le=11)] | None

    @model_validator(mode="after")
    def semantics(self):
        if (self.accessibility_label is None) != (self.semantics_order is None):
            raise ValueError("image label and reading order must both be null or present")
        return self


class BannerAuthoredText(BannerText):
    text: TextExpression


class BannerAuthoredButton(BannerButton):
    text: TextExpression


Element = Annotated[BannerText | BannerButton | BannerImage, Field(discriminator="type")]
AuthoredElement = Annotated[
    BannerAuthoredText | BannerAuthoredButton | BannerImage, Field(discriminator="type")
]


class BannerCanvas(BannerModel):
    background: Background
    radius: Annotated[Scalar, Field(ge=0, le=24)]
    border: BannerBorder
    elements: Annotated[tuple[Element, ...], Field(max_length=12)]

    @model_validator(mode="after")
    def unique_elements(self):
        ids = [e.id for e in self.elements]
        orders = [e.semantics_order for e in self.elements if e.semantics_order is not None]
        if len(set(ids)) != len(ids) or len(set(orders)) != len(orders):
            raise ValueError("duplicate element id or reading order")
        if sum(e.type == "button_v1" for e in self.elements) > 2:
            raise ValueError("too many buttons")
        images = sum(e.type == "image_v1" for e in self.elements)
        if images + (self.background.type == "image_background_v1") > 2:
            raise ValueError("too many images")
        return self


class BannerAuthoredCanvas(BannerCanvas):
    elements: Annotated[tuple[AuthoredElement, ...], Field(max_length=12)]


class BannerBinding(BannerModel):
    source: Literal["user.local_date", "routines.remaining_today"]
    format: Literal["month_day", "full_date"] | None

    @model_validator(mode="after")
    def source_format(self):
        if (self.source == "user.local_date") != (self.format is not None):
            raise ValueError("date requires format; count requires null format")
        return self


class BannerWhen(BannerModel):
    binding: str
    operator: Literal["eq", "gt"]
    value: Annotated[int, Field(ge=0)]


def version_core(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(
        r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", value
    )
    return tuple(map(int, match.groups())) if match else None


class BannerDefinition(BannerModel):
    id: Id
    component: Literal["banner_canvas_v1"]
    layout_profile: Literal["home_blind_v1"]
    enabled: bool
    starts_at: datetime | None
    ends_at: datetime | None
    platforms: Annotated[tuple[Literal["android", "ios"], ...], Field(min_length=1, max_length=2)]
    min_app_version: Annotated[str, Field(max_length=64)] | None
    max_app_version_exclusive: Annotated[str, Field(max_length=64)] | None
    bindings: dict[str, BannerBinding]
    when: BannerWhen | None
    canvases_by_locale: dict[Literal["en", "ko", "ja"], BannerAuthoredCanvas]

    @model_validator(mode="after")
    def definition(self):
        if "en" not in self.canvases_by_locale:
            raise ValueError("English canvas is required")
        if len(set(self.platforms)) != len(self.platforms):
            raise ValueError("duplicate platform")
        for instant in (self.starts_at, self.ends_at):
            if instant is not None and (
                instant.tzinfo is None or instant.utcoffset() != timedelta(0)
            ):
                raise ValueError("schedule must use UTC")
        if self.starts_at and self.ends_at and self.starts_at >= self.ends_at:
            raise ValueError("schedule is empty")
        for version in (self.min_app_version, self.max_app_version_exclusive):
            if version is not None and version_core(version) is None:
                raise ValueError("invalid app version")
        if self.min_app_version and self.max_app_version_exclusive:
            if version_core(self.min_app_version) >= version_core(self.max_app_version_exclusive):
                raise ValueError("version interval is empty")
        for alias in self.bindings:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", alias):
                raise ValueError("invalid binding alias")
        if self.when:
            bound = self.bindings.get(self.when.binding)
            if not bound or bound.source != "routines.remaining_today":
                raise ValueError("condition requires integer binding")
        if any(b.source == "routines.remaining_today" for b in self.bindings.values()):
            if not self.when or self.when.operator != "gt" or self.when.value != 0:
                raise ValueError("routine-dependent banners require remaining > 0")
        for canvas in self.canvases_by_locale.values():
            for element in canvas.elements:
                if isinstance(element, BannerImage):
                    continue
                expression = element.text
                templates = [expression]
                if isinstance(expression, BannerCountCases):
                    binding = self.bindings.get(expression.binding)
                    if not binding or binding.source != "routines.remaining_today":
                        raise ValueError("count_cases requires integer binding")
                    templates = [expression.zero, expression.one, expression.other]
                for template in templates:
                    if not template_aliases(template.value) <= self.bindings.keys():
                        raise ValueError("unknown binding alias")
        return self


class BannerManifest(BannerModel):
    manifest_format_version: Literal[1]
    wire_schema_version: Literal[1]
    placement: Literal["home_blind"]
    enabled: bool
    banners: Annotated[tuple[BannerDefinition, ...], Field(max_length=50)]

    @model_validator(mode="after")
    def unique_banners(self):
        if len({b.id for b in self.banners}) != len(self.banners):
            raise ValueError("duplicate banner id")
        return self


class BannerCard(BannerModel):
    data_dependencies: tuple[Literal["user.local_date", "routines.remaining_today"], ...]
    id: Id
    component: Literal["banner_canvas_v1"]
    layout_profile: Literal["home_blind_v1"]
    locale: Literal["en", "ko", "ja"]
    valid_until: datetime | None
    canvas: BannerCanvas


class BannerFeed(BannerModel):
    schema_version: Literal[1]
    placement: Literal["home_blind"]
    revision: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    served_at: datetime
    items: Annotated[tuple[BannerCard, ...], Field(max_length=5, json_schema_extra={"items": {}})]
