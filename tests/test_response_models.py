"""모든 JSON 성공 라우트와 대표 응답이 구체 모델로 고정되는지 검증."""
from __future__ import annotations

from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError
import pytest

from app.main import app
from app.schemas.ads import AdSsvResponse, RewardAdSessionResponse
from app.schemas.chat import ChatStateResponse, MessagesResponse, PostMessageResponse
from app.schemas.common import HealthResponse, StatusResponse
from app.schemas.dev import DiaryGenerateResponse
from app.schemas.diary import DiaryDetailResponse, DiaryListResponse
from app.schemas.economy import (
    ChargingStationResponse,
    RewardResponse,
    TransactionsResponse,
    WalletResponse,
)
from app.schemas.routine import (
    RoutineCompleteResponse,
    RoutineListResponse,
    RoutineResponse,
    RoutineStatisticsResponse,
)
from app.schemas.subscription import SubscriptionPlansResponse, SubscriptionResponse

NOW = "2026-07-16T00:00:00+00:00"
UUID = "11111111-1111-1111-1111-111111111111"
ROUTINE = {
    "id": UUID,
    "name": "산책",
    "frequency_per_week": 3,
    "days_of_week": [1, 3, 5],
    "reminder_enabled": True,
    "reminder_time": "09:30",
    "completed_today": False,
}


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (HealthResponse, {"status": "ok", "app": "moly-backend", "env": "local"}),
        (
            ChatStateResponse,
            {
                "activity_date": "2026-07-16",
                "plan": "trial",
                "tokens_used": 10,
                "daily_token_limit": 30_000,
                "tokens_remaining": 29_990,
                "warning_threshold": 3_000,
                "personal_diary_eligible": False,
                "limit_reached": False,
            },
        ),
        (MessagesResponse, {"data": [], "older_cursor": None, "newer_cursor": None}),
        (
            PostMessageResponse,
            {
                "greeting": None,
                "user_message": {"message_id": "1", "created_at": NOW},
                "reply": {"message_id": "2", "content": "응", "created_at": NOW},
                "tokens_used": 20,
                "tokens_remaining": 29_980,
                "review_prompt": False,
            },
        ),
        (DiaryListResponse, {"data": [], "next_cursor": None}),
        (
            DiaryDetailResponse,
            {
                "id": UUID,
                "diary_date": "2026-07-15",
                "type": "personal",
                "title": None,
                "weather": "sunny",
                "body": "본문",
                "conversation_ref": {"anchor_date": "2026-07-15"},
                "published_at": NOW,
                "first_read_at": None,
            },
        ),
        (WalletResponse, {"balance": 100}),
        (TransactionsResponse, {"data": [], "next_cursor": None}),
        (
            ChargingStationResponse,
            {
                "activity_date": "2026-07-16",
                "attendance": {"claimable": True, "claimed": False, "reward": 10},
                "ad": {"views_used": 0, "views_limit": 10, "reward_per_view": 10},
                "routine_pair": {
                    "completed_today": 0,
                    "required": 2,
                    "claimable": False,
                    "claimed": False,
                    "reward": 10,
                },
                "hay_products": [{"product_id": None, "amount": None}],
                "balance": 100,
            },
        ),
        (RewardResponse, {"granted": 10, "balance_after": 110}),
        (RoutineResponse, ROUTINE),
        (RoutineListResponse, {"data": [ROUTINE]}),
        (RoutineCompleteResponse, {"completed_today": True, "completed_count_today": 1}),
        (
            RoutineStatisticsResponse,
            {
                "streak": 1,
                "completed_today": True,
                "target_count": 3,
                "days_of_week": [1, 3, 5],
                "this_week": {
                    "completed_count": 1,
                    "by_weekday": {str(day): day == 3 for day in range(1, 8)},
                },
                "last_30_days": ["2026-07-16"],
                "completion_rate": 0.25,
            },
        ),
        (
            SubscriptionResponse,
            {
                "status": "none",
                "plan": None,
                "auto_renew_enabled": False,
                "expires_at": None,
                "in_trial": True,
                "trial_ends_at": NOW,
            },
        ),
        (
            SubscriptionPlansResponse,
            {
                "plans": [
                    {"product_id": "app.moly.sub.monthly", "period": "monthly", "hay_grant": 1000}
                ],
                "benefits": ["대화 한도 확장"],
            },
        ),
        (StatusResponse, {"status": "ok"}),
        (
            RewardAdSessionResponse,
            {
                "reward_session_id": UUID,
                "admob_user_id": UUID,
                "views_used": 0,
                "views_limit": 10,
            },
        ),
        (AdSsvResponse, {"status": "ok", "result": "granted"}),
        (
            DiaryGenerateResponse,
            {
                "target_date": "2026-07-15",
                "diagnostics": {
                    "created": False,
                    "skipped": True,
                    "reason": "already_exists",
                    "hint": "이미 일기가 있어 스킵됨.",
                },
                "diary": None,
            },
        ),
    ],
)
def test_representative_response_payloads(model, payload):
    model.model_validate(payload)


def test_utc_datetime_keeps_offset_wire_format():
    """서비스가 내던 isoformat(+00:00)이 response_model 직렬화 후에도 'Z'로 안 바뀌어야 한다."""
    dumped = PostMessageResponse.model_validate(
        {
            "greeting": None,
            "user_message": {"message_id": "1", "created_at": NOW},
            "reply": {"message_id": "2", "content": "응", "created_at": NOW},
            "tokens_used": 20,
            "tokens_remaining": 29_980,
            "review_prompt": False,
        }
    ).model_dump(mode="json")
    assert dumped["user_message"]["created_at"] == NOW
    assert dumped["reply"]["created_at"] == NOW


def test_all_json_success_routes_use_concrete_base_models():
    missing = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.status_code == 204:
            continue
        response_model = route.response_model
        if not (
            isinstance(response_model, type)
            and issubclass(response_model, BaseModel)
        ):
            missing.append(f"{','.join(sorted(route.methods or []))} {route.path}")
    assert missing == []


def test_response_models_reject_unknown_fields_and_invalid_enums():
    with pytest.raises(ValidationError):
        HealthResponse.model_validate(
            {"status": "ok", "app": "moly-backend", "env": "local", "extra": True}
        )
    with pytest.raises(ValidationError):
        SubscriptionResponse.model_validate(
            {
                "status": "unknown",
                "plan": None,
                "auto_renew_enabled": False,
                "expires_at": None,
                "in_trial": False,
                "trial_ends_at": None,
            }
        )
    with pytest.raises(ValidationError):
        PostMessageResponse.model_validate({"reply": {"content": "필드 누락"}})
