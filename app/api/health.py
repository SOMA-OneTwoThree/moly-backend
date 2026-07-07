from fastapi import APIRouter

from app.config import settings

router = APIRouter(tags=["system"])


@router.get("/health")
def health_check() -> dict[str, str]:
    """헬스체크 — 인증 불필요(로드밸런서/배포 프로브)."""
    return {"status": "ok", "app": settings.app_name, "env": settings.environment}
