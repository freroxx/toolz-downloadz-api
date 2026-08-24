import logging
import sys
import time
from typing import Callable
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
import uuid


def setup_logging(level: str = "info"):
    lvl = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    # Reduce noisy libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


logger = logging.getLogger("toolz-api")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        req_id = str(uuid.uuid4())[:8]
        start = time.time()
        # Add request ID to state for downstream use
        request.state.request_id = req_id
        try:
            response = await call_next(request)
            duration_ms = int((time.time() - start) * 1000)
            logger.info(
                f"[{req_id}] {request.method} {request.url.path}?{request.url.query} -> {response.status_code} {duration_ms}ms "
                f"ip={request.client.host if request.client else '?'} ua={request.headers.get('user-agent','')[:60]}"
            )
            response.headers["X-Request-ID"] = req_id
            return response
        except Exception as e:
            duration_ms = int((time.time() - start) * 1000)
            logger.exception(f"[{req_id}] {request.method} {request.url.path} failed after {duration_ms}ms: {e}")
            raise
