from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import Request
from fastapi.responses import JSONResponse

# Use IP + API key as identifier for more fair limiting per user
def _key_func(request: Request) -> str:
    # Prefer API key if present, else IP
    key = request.headers.get("x-api-key") or request.headers.get("authorization") or ""
    if key:
        # Use hashed-ish key (first 8 chars) to avoid log spill but still separate
        return f"key:{key[:16]}"
    return get_remote_address(request)

limiter = Limiter(key_func=_key_func, storage_uri="memory://", strategy="fixed-window")


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={
            "detail": f"Rate limit exceeded: {exc.detail}. Try again in a moment.",
            "retry_after": getattr(exc, "retry_after", None),
        },
        headers={"Retry-After": str(getattr(exc, "retry_after", 60))},
    )
