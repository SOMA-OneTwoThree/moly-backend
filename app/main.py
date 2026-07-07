from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import settings


def create_app() -> FastAPI:
    """API 앱 팩토리. 모듈 라우터는 여기서 등록(auth·chat·diary… 는 구현 시 추가)."""
    app = FastAPI(title=settings.app_name)
    app.include_router(health_router)
    return app


app = create_app()
