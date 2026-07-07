from fastapi import FastAPI

from app.api.app_config import router as app_config_router
from app.api.health import router as health_router
from app.config import settings
from app.core.errors import register_error_handlers


def create_app() -> FastAPI:
    """API 앱 팩토리. 모듈 라우터는 여기서 등록(auth·chat·diary… 는 구현 시 추가)."""
    app = FastAPI(title=settings.app_name)
    register_error_handlers(app)
    # 공개(인증 불필요): 헬스체크 · 부팅 설정
    app.include_router(health_router)
    app.include_router(app_config_router)
    return app


app = create_app()
