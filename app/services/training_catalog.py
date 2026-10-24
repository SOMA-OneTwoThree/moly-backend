"""탐색 트레이닝 원고 — 저장소의 JSON을 한 번 검증해 메모리에 둔다. 실행 중에 바꾸지 않는다.

정답·힌트·풀이·채점 기준은 서버만 쓰고, 앱에 내려주는 공개 보기(`public_stage`)에는 넣지 않는다.
공개한 뒤에는 트레이닝 id·단계 번호·보기 id를 바꾸지 않는다. 저장된 답이 보기 id를 가리키기
때문이다. 문구 손질은 괜찮고, 구조를 바꿔야 하면 새 트레이닝 id로 낸다.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CATALOG_PATH = Path(__file__).resolve().parents[1] / "resources/trainings/catalog.json"
MAX_BYTES = 2 * 1024 * 1024
MAX_STAGES = 30
ANSWER_MAX_LENGTH = 200  # 코드 포인트. DB char_length·앱 입력 제한과 같은 단위다.

TrainingId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")]
OptionId = Annotated[str, Field(pattern=r"^[a-z0-9]{1,16}$")]
IconKey = Annotated[str, Field(pattern=r"^[a-z0-9_]{1,64}$")]
Copy = Annotated[str, Field(min_length=1, max_length=600)]

_log = logging.getLogger("moly-backend")


class TrainingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class LocalizedText(TrainingModel):
    ko: Copy
    en: Copy
    ja: Copy

    @field_validator("ko", "en", "ja")
    @classmethod
    def plain_text(cls, value: str) -> str:
        if value != value.strip() or any(unicodedata.category(c) == "Cc" for c in value):
            raise ValueError("copy must be trimmed plain text without control characters")
        if value != unicodedata.normalize("NFC", value):
            raise ValueError("copy must use NFC normalization")
        return value

    def pick(self, locale: str) -> str:
        return getattr(self, locale)


class TrainingLine(TrainingModel):
    """상황 이야기 한 줄. `slot`은 답 자리이고, 그 줄의 `text`는 빈칸 앞에 붙는 말이다."""

    speaker: Literal["narration", "other", "me"]
    name: LocalizedText | None = None
    text: LocalizedText | None = None
    slot: bool = False

    @model_validator(mode="after")
    def consistent(self) -> TrainingLine:
        if (self.speaker == "other") != (self.name is not None):
            raise ValueError("only lines spoken by another person carry a name")
        if self.slot and self.speaker == "other":
            raise ValueError("the answer slot belongs to the user or the narration")
        if not self.slot and self.text is None:
            raise ValueError("a line without the answer slot needs text")
        return self


class TrainingOption(TrainingModel):
    id: OptionId
    text: LocalizedText
    correct: bool = False
    hint: LocalizedText | None = None

    @model_validator(mode="after")
    def hint_on_wrong_options(self) -> TrainingOption:
        if self.correct == (self.hint is not None):
            raise ValueError("every wrong option needs a hint and the correct option has none")
        return self


class TrainingGrading(TrainingModel):
    """서버 전용 채점 기준. 기존 과정의 평가용 예시는 프롬프트에 보내지 않는다."""

    criteria: Annotated[str, Field(min_length=1, max_length=1000)]
    example: LocalizedText | None = None
    topic: Literal["refusal"] | None = None
    retry_hint: LocalizedText | None = None


class TrainingStage(TrainingModel):
    icon_key: IconKey | None = None
    number: Annotated[int, Field(ge=1, le=MAX_STAGES)]
    answer_type: Literal["pick", "write"]
    layout: Literal["chat", "diary"]
    pick_style: Literal["choose", "erase"] | None = None
    review_point: bool = False
    reward_hay: Annotated[int, Field(ge=0, le=1000)] = 0
    mission: LocalizedText
    lines: Annotated[tuple[TrainingLine, ...], Field(min_length=1, max_length=20)]
    options: Annotated[tuple[TrainingOption, ...], Field(max_length=4)] = ()
    explanation: LocalizedText | None = None
    grading: TrainingGrading | None = None

    @model_validator(mode="after")
    def consistent(self) -> TrainingStage:
        slots = sum(line.slot for line in self.lines)
        if slots > 1 or (slots == 0 and (self.answer_type == "write" or self.pick_style == "erase")):
            raise ValueError("write/erase needs one slot; choose allows zero or one")
        if (self.reward_hay > 0) != self.review_point:
            raise ValueError("only review points grant hay")
        if self.answer_type == "write":
            if self.pick_style or self.options or self.grading is None:
                raise ValueError("write stages require grading and cannot carry pick options or style")
            return self
        if self.pick_style is None or self.grading is not None:
            raise ValueError("pick stages need a pick style and no grading")
        if self.pick_style == "erase" and self.layout != "diary":
            raise ValueError("erasing a sentence is a diary stage")
        if not (3 if self.pick_style == "erase" else 2) <= len(self.options) <= 4:
            raise ValueError("option count out of range")
        if sum(option.correct for option in self.options) != 1:
            raise ValueError("pick stages need exactly one correct option")
        if len({option.id for option in self.options}) != len(self.options):
            raise ValueError("duplicate option id")
        return self

    def option(self, option_id: str) -> TrainingOption | None:
        return next((option for option in self.options if option.id == option_id), None)


class TrainingDefinition(TrainingModel):
    id: TrainingId
    archived: bool = False
    order: Annotated[int, Field(ge=0, le=1000)]
    icon_key: IconKey
    title: LocalizedText
    summary: LocalizedText
    stages: Annotated[tuple[TrainingStage, ...], Field(min_length=1, max_length=MAX_STAGES)]

    @model_validator(mode="after")
    def numbered(self) -> TrainingDefinition:
        if [stage.number for stage in self.stages] != list(range(1, len(self.stages) + 1)):
            raise ValueError("stages must be numbered from 1 without gaps")
        return self

    def stage(self, number: int) -> TrainingStage | None:
        return self.stages[number - 1] if 1 <= number <= len(self.stages) else None


class TrainingManifest(TrainingModel):
    schema_version: Literal[1]
    trainings: Annotated[tuple[TrainingDefinition, ...], Field(max_length=50)]

    @model_validator(mode="after")
    def unique_ids(self) -> TrainingManifest:
        if len({training.id for training in self.trainings}) != len(self.trainings):
            raise ValueError("duplicate training id")
        return self


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-finite JSON constant")


@dataclass(frozen=True)
class TrainingCatalog:
    trainings: tuple[TrainingDefinition, ...]  # 노출 순서
    by_id: Mapping[str, TrainingDefinition]

    @classmethod
    def from_bytes(cls, raw: bytes) -> TrainingCatalog:
        if len(raw) > MAX_BYTES:
            raise ValueError("training catalog byte budget exceeded")
        json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                   parse_constant=_invalid_constant)
        manifest = TrainingManifest.model_validate_json(raw)
        ordered = tuple(sorted(manifest.trainings, key=lambda t: (t.order, t.id)))
        return cls(ordered, MappingProxyType({t.id: t for t in ordered}))

    @classmethod
    def load(cls, path: Path = CATALOG_PATH) -> TrainingCatalog:
        with path.open("rb") as stream:
            return cls.from_bytes(stream.read(MAX_BYTES + 1))

    def get(self, training_id: str) -> TrainingDefinition | None:
        return self.by_id.get(training_id)


EMPTY = TrainingCatalog((), MappingProxyType({}))


@lru_cache(maxsize=1)
def current() -> TrainingCatalog:
    """서비스가 쓰는 원고. 파일이 없으면 빈 목록이고, 깨졌으면 로그를 남기고 빈 목록이다.

    빈 목록이면 앱은 트레이닝 섹션을 숨긴다. 깨진 원고는 CI의 원고 테스트가 먼저 막는다.
    """
    try:
        return TrainingCatalog.load()
    except FileNotFoundError:
        return EMPTY
    except (OSError, ValueError) as exc:
        _log.error("training catalog unavailable: %s", type(exc).__name__)
        return EMPTY


def _public_line(line: TrainingLine, locale: str) -> dict[str, Any]:
    return {
        "speaker": line.speaker,
        "name": line.name.pick(locale) if line.name else None,
        "text": line.text.pick(locale) if line.text else None,
        "slot": line.slot,
    }


def public_stage(stage: TrainingStage, locale: str) -> dict[str, Any]:
    """앱에 내려줄 단계. 정답 표시·힌트·풀이·채점 기준은 넣지 않는다."""
    return {
        "number": stage.number,
        "icon_key": stage.icon_key,
        "answer_type": stage.answer_type,
        "layout": stage.layout,
        "pick_style": stage.pick_style,
        "review_point": stage.review_point,
        "reward_hay": stage.reward_hay,
        "mission": stage.mission.pick(locale),
        "lines": [_public_line(line, locale) for line in stage.lines],
        "options": [{"id": option.id, "text": option.text.pick(locale)} for option in stage.options],
        "max_length": ANSWER_MAX_LENGTH if stage.answer_type == "write" else None,
    }
