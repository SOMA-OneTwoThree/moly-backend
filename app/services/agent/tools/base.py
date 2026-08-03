"""W6 도구 공통 기반 — 인자 재검증·문자 상한·wire 스키마·결과 포장.

도구 구현체는 `BaseTool.run()`만 채운다. 인자 파싱, 상한 위반 처리, `ToolResult` 조립,
wire serialize 규칙(UUID canonical / date `YYYY-MM-DD` / datetime UTC ISO-8601 `Z`)은 전부 여기 있다.
같은 규칙을 도구마다 다시 쓰면 한 곳만 어긋나도 모델이 보는 표면이 갈라진다.

인자는 **두 번** 검증된다. 한 번은 W4(`llm._parse_call`)가 registry의 `input_models`로,
또 한 번은 여기서다. 중복이 아니라 계층이 다르다 — 도구는 LLM 경로 밖(테스트·후속 호출자)에서도
불릴 수 있고, 그때 검증이 통째로 빠지면 안 된다.

`ToolResult.call_id`/`tool_name`은 런타임(`runtime._normalize`)이 실제 호출 값으로 덮으므로
여기서는 빈 문자열로 둔다 — 도구가 call_id를 지어내면 그게 곧 형식 미완결이다.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, PlainSerializer, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.agent.runtime import ToolContext
from app.services.llm import ToolResult
from app.services.memory import sanitize_text

_log = logging.getLogger("moly-backend")

# 잘린 문자열 끝에 붙는 표시. runtime._truncate_strings와 같은 문자를 쓴다(표면 일관).
ELLIPSIS = "…"


def _utc_z(value: dt.datetime) -> str:
    """datetime → UTC ISO-8601 `Z`. naive는 UTC로 간주한다(DB는 timestamptz라 정상경로엔 없다)."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)
    return aware.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# 모델 wire에 나가는 datetime 타입. pydantic 기본 직렬화는 `+00:00`이라 `Z`로 고정한다.
UtcDatetime = Annotated[dt.datetime, PlainSerializer(_utc_z, return_type=str)]


class ToolArgs(BaseModel):
    """도구 인자 모델의 공통 설정.

    `extra="forbid"` = JSON 스키마의 `additionalProperties:false`. 모델이 지어낸 필드(특히
    `user_id`류)를 조용히 흘려보내지 않고 schema_mismatch로 떨어뜨린다.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class InvalidArguments(ValueError):
    """인자가 계약 위반(`from > to`·조회 범위 초과). 실행하지 않고 invalid_arguments로 닫는다.

    pydantic이 잡는 타입·길이와 달리 이쪽은 `ctx.activity_date` 기준 상대 범위라 실행 시점에만
    판정할 수 있다.
    """


def clip(text: str | None, cap: int) -> tuple[str, bool]:
    """살균 후 **cap자 이하**로 자른다. 반환 = (문자열, 잘렸는지).

    살균이 **먼저**다 — 자르고 살균하면 제거 대상 문자가 자리만 차지하고 사라져 실제 길이가
    상한보다 짧아진다(상한이 의미를 잃는다).

    말줄임표까지 포함해 cap을 넘지 않는다. 넘기면 "전체 N자" 예산을 항목 수만큼 초과하게 된다.
    """
    s = sanitize_text(text or "")
    if cap <= 0:
        return "", bool(s)
    if len(s) <= cap:
        return s, False
    return s[: cap - 1] + ELLIPSIS, True


def ok_result(tool: str, data: Any, *, truncated: bool = False) -> ToolResult:
    """성공 결과. `not_found`는 실패가 아니므로 빈 결과도 여기로 온다(status='ok')."""
    return ToolResult(
        call_id="",  # runtime._normalize가 실제 call_id로 덮는다
        tool_name=tool,
        status="ok",
        data=data,
        truncated=truncated,
    )


def unavailable(tool: str, error_code: str) -> ToolResult:
    """실패 결과. DB·의존성 실패와 인자 위반만 여기 온다(빈 조회 결과는 아니다)."""
    return ToolResult(
        call_id="", tool_name=tool, status="unavailable", data=None, error_code=error_code
    )


class BaseTool:
    """도구 공통 실행 껍데기. 구현체는 `run()`만 채운다.

    `run()`은 (output_model 인스턴스, truncated)를 돌려주고, 여기서 wire dict로 직렬화한다.
    DB 예외는 잡지 않는다 — `runtime._run_one`이 SQLAlchemyError를 dependency_error로 닫는 게
    이미 계약이고, 여기서 삼키면 그 분류가 사라진다.
    """

    name: str = ""
    description: str = ""
    input_model: type[ToolArgs]
    output_model: type[BaseModel]
    timeout_ms: int | None = None  # None이면 config의 상한(agent_tool_timeout_ms)

    async def run(
        self, ctx: ToolContext, args: Any, session: AsyncSession
    ) -> tuple[BaseModel, bool]:  # pragma: no cover - 추상
        raise NotImplementedError

    async def execute(
        self, ctx: ToolContext, args: Mapping[str, Any], session: AsyncSession
    ) -> ToolResult:
        try:
            parsed = self.input_model.model_validate(dict(args))
        except ValidationError:
            # W4가 이미 걸렀어야 하는 경로다(=여기 오면 LLM 밖에서 불린 것). 인자 값은 남기지 않는다.
            _log.info("도구 인자 스키마 불일치 tool=%s", self.name)
            return unavailable(self.name, "invalid_arguments")
        try:
            out, truncated = await self.run(ctx, parsed, session)
        except InvalidArguments as e:
            _log.info("도구 인자 범위 위반 tool=%s reason=%s", self.name, e)
            return unavailable(self.name, "invalid_arguments")
        return ok_result(self.name, out.model_dump(mode="json"), truncated=truncated)


def wire_schema(tool: BaseTool) -> dict:
    """도구 → OpenAI function 스키마. name/description은 **ASCII 고정 영어**다.

    언어별로 분기하면 프리픽스 캐시가 언어 수만큼 쪼개진다(§3.1 비용 근거). 그래서 유저 언어는
    도구가 반환하는 **데이터**에만 반영하고(루틴명 i18n) 스키마 자체는 고정이다.
    """
    schema = tool.input_model.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": schema,
        },
    }


def offset_days(target: dt.date, anchor: dt.date) -> int:
    return abs((target - anchor).days)
