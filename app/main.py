from fastapi import FastAPI

from app.api.account import router as account_router
from app.api.chat import router as chat_router
from app.api.diary import router as diary_router
from app.api.economy import router as economy_router
from app.api.health import router as health_router
from app.api.review import router as review_router
from app.api.routine import router as routine_router
from app.api.shop import router as shop_router
from app.config import settings
from app.core.errors import register_error_handlers


def create_app() -> FastAPI:
    """API 앱 팩토리. 모듈 라우터는 여기서 등록(chat·diary… 는 구현 시 추가)."""
    # OpenAPI 문서는 로컬에서만 노출(프로덕션 무인증 스키마 열람 차단).
    _local = settings.environment == "local"
    app = FastAPI(
        title=settings.app_name,
        docs_url="/docs" if _local else None,
        redoc_url="/redoc" if _local else None,
        openapi_url="/openapi.json" if _local else None,
    )
    register_error_handlers(app)
    # 공개(인증 불필요): 헬스체크만. (부팅 설정/강제업데이트/점검/낮밤은 Firebase로 이관)
    app.include_router(health_router)
    # 인증 필요: 각 엔드포인트가 get_current_user 의존
    app.include_router(account_router)
    app.include_router(chat_router)
    app.include_router(diary_router)
    app.include_router(economy_router)
    app.include_router(routine_router)
    app.include_router(shop_router)
    app.include_router(review_router)
    return app


app = create_app()
