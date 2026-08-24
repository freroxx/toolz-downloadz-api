import os
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from slowapi.errors import RateLimitExceeded

# Load env early
load_dotenv()
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from .core.config import get_settings
from .core.logging import setup_logging, RequestLoggingMiddleware, logger
from .core.rate_limit import limiter, rate_limit_exceeded_handler
from .routers import extract as extract_router
from .routers import health as health_router

settings = get_settings()
setup_logging(settings.log_level)

# Optional Sentry
if settings.sentry_dsn:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            traces_sample_rate=0.1,
            environment=settings.environment,
        )
        logger.info("Sentry enabled")
    except Exception as e:
        logger.warning(f"Sentry init failed: {e}")

app = FastAPI(
    title="toolz-downloadz-api",
    description="Production-ready media extraction API — yt-dlp orchestrator with caching, fallbacks, and hardening",
    version=settings.version,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    contact={"name": "Toolz", "url": "https://github.com/freroxx/toolz-downloadz"},
)

# Rate limiter state
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)  # type: ignore

# CORS — use configured origins
origins = settings.allowed_origins
# If "*", CORSMiddleware needs allow_origins=["*"] and allow_credentials=False (spec). Handle.
allow_credentials = True
if origins == ["*"]:
    allow_credentials = False  # "*"" with credentials not allowed by spec

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-Cache", "Retry-After"],
)

# Request logging
app.add_middleware(RequestLoggingMiddleware)

# Routers — primary at /api for Vercel compat
app.include_router(health_router.router, prefix="/api", tags=["health"])
app.include_router(extract_router.router, prefix="/api", tags=["extract"])

# Convenience aliases without /api prefix (e.g. /health, /extract)
# Thin wrappers to avoid duplicate operationId collisions.
from fastapi import Depends

@app.get("/health", include_in_schema=False)
async def health_alias():
    from .routers.health import health_check
    return await health_check()

@app.get("/platforms", include_in_schema=False)
async def platforms_alias():
    from .routers.extract import get_platforms
    return await get_platforms()


@app.get("/", include_in_schema=False)
async def root():
    return {
        "name": settings.app_name,
        "version": settings.version,
        "docs": "/docs",
        "health": "/api/health",
        "extract": "/api/extract?url=https://...",
    }


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    # Log unhandled
    logger.exception(f"Unhandled error at {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "request_id": getattr(request.state, "request_id", None)},
    )


# For Vercel: expose app
# Vercel expects api/index.py to have `app`, we keep compatibility via import
