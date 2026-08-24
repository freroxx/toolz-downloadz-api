import time
from fastapi import APIRouter
from ..core.config import get_settings

router = APIRouter(tags=["health"])

_started = time.time()


@router.get("/health", summary="Health check")
async def health_check():
    settings = get_settings()
    uptime = int(time.time() - _started)
    return {
        "status": "online",
        "service": settings.app_name,
        "version": settings.version,
        "environment": settings.environment,
        "uptime_seconds": uptime,
        "cache": "redis" if settings.redis_url else "memory",
    }


@router.get("/healthz", include_in_schema=False)
async def healthz():
    return {"ok": True}


@router.get("/ready", include_in_schema=False)
async def ready():
    # Could check yt-dlp, redis, etc
    return {"ready": True}
