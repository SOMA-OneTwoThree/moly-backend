from fastapi import FastAPI

from app.api.ads import router as ads_router
from app.api.chat import router as chat_router
from app.api.diary import router as diary_router
from app.api.economy import router as economy_router
from app.api.feedback import router as feedback_router
from app.api.health import router as health_router
from app.api.review import router as review_router
from app.api.routine import router as routine_router
from app.api.shop import router as shop_router
from app.api.subscription import router as subscription_router
from app.config import settings
from app.core.errors import register_error_handlers


def create_app() -> FastAPI:
    """API 앱 팩토리. 모듈 라우터는 여기서 등록(chat·diary… 는 구현 시 추가)."""
    # 비-local이면 StoreKit 결제/웹훅 설정 강제(누락 시 부팅 실패, 서명검증 우회 방지).
    settings.require_production_ready()
    _local = settings.environment == "local"
    # Swagger/OpenAPI는 로컬 전용(개발 테스트용). 프로덕션은 전부 None으로 노출 안 함.
    app = FastAPI(
        title=settings.app_name,
        docs_url="/docs" if _local else None,
        redoc_url=None,
        openapi_url="/openapi.json" if _local else None,
    )
    register_error_handlers(app)
    # 공개(인증 불필요): 헬스체크만. (부팅 설정/강제업데이트/점검/낮밤은 Firebase로 이관)
    app.include_router(health_router)
    # 인증 필요: 각 엔드포인트가 get_current_user 의존
    # (계정 API — /me·/onboarding·알림·푸시토큰·로그아웃·탈퇴 — 는 moly-auth 서버 소유)
    app.include_router(chat_router)
    app.include_router(diary_router)
    app.include_router(economy_router)
    app.include_router(routine_router)
    app.include_router(shop_router)
    app.include_router(review_router)
    app.include_router(feedback_router)
    app.include_router(subscription_router)
    app.include_router(ads_router)
    # 로컬 전용: 워커 배치(일기 생성)를 curl로 손으로 돌리는 개발 라우터.
    # 프로덕션엔 라우트 자체가 등록되지 않는다.
    if _local:
        from app.api.dev import router as dev_router

        app.include_router(dev_router)
    return app


app = create_app()
