import asyncio
import contextlib
import time

from fastapi import FastAPI, Request

from app.api.ads import router as ads_router
from app.api.attribution import router as attribution_router
from app.api.chat import router as chat_router
from app.api.diary import router as diary_router
from app.api.economy import router as economy_router
from app.api.feedback import router as feedback_router
from app.api.fortune import router as fortune_router
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
    # Swagger/OpenAPI는 로컬과 격리된 개발 서버에서만 노출한다. dev는 실제 인증·DB를 붙인
    # 수동 대화 검증 표면이고, staging/prod/알 수 없는 환경은 계속 fail-closed다.
    _docs_enabled = settings.environment in {"local", "development"}
    # #23b: 원장 close 배치 flush — 챗 lane의 close_call이 버퍼로 가므로, API 프로세스에도
    # flusher와 **graceful shutdown flush**가 반드시 있어야 한다(consumer만 있으면 챗 close가
    # 프로세스 종료 때 유실돼 reconciler(24h)까지 unknown으로 밀린다).
    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI):
        from app.services import usage_ledger

        stop = asyncio.Event()
        flusher = asyncio.ensure_future(usage_ledger.run_close_flusher(stop))
        try:
            yield
        finally:
            stop.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(flusher, timeout=10.0)

    app = FastAPI(
        title=settings.app_name,
        docs_url="/docs" if _docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if _docs_enabled else None,
        lifespan=_lifespan,
    )
    register_error_handlers(app)

    @app.middleware("http")
    async def request_clock(request: Request, call_next):  # type: ignore[no-untyped-def]
        # 인증 dependency와 DB 대기까지 포함한 절대 HTTP 예산의 시작점.
        request.state.started_monotonic = time.monotonic()
        return await call_next(request)
    # 공개(인증 불필요): 헬스체크와 설치 귀속 복호화.
    # (부팅 설정/강제업데이트/점검/낮밤은 Firebase로 이관)
    # 설치 리퍼러 복호화는 설치 직후 로그인 전에 호출되므로 인증을 걸 수 없다.
    app.include_router(health_router)
    app.include_router(attribution_router)
    # 인증 필요: 각 엔드포인트가 get_current_user 의존
    # (계정 API — /me·/onboarding·알림·푸시토큰·로그아웃·탈퇴 — 는 moly-auth 서버 소유)
    app.include_router(chat_router)
    app.include_router(diary_router)
    app.include_router(economy_router)
    app.include_router(routine_router)
    app.include_router(shop_router)
    app.include_router(review_router)
    app.include_router(feedback_router)
    app.include_router(fortune_router)
    app.include_router(subscription_router)
    app.include_router(ads_router)
    # 로컬/격리 개발 서버 전용: 워커·회상·모델 평가를 Swagger에서 손으로 검증한다.
    # 명시 플래그와 환경 allowlist를 모두 만족해야 하므로 production·staging·알 수 없는 환경은
    # 플래그가 잘못 켜져도 라우트 자체가 등록되지 않는다.
    if settings.enable_dev_routes and settings.environment in {"local", "development"}:
        from app.api.dev import router as dev_router

        app.include_router(dev_router)
    return app


app = create_app()
