from fastapi import APIRouter, HTTPException, Depends, Request, Query, Body
from typing import Optional, Dict, Any
import asyncio

from ..core.security import verify_api_key, validate_url
from ..core.rate_limit import limiter
from ..core.config import get_settings
from ..core.cache import cache_get, cache_set
from ..services.extractor import extract_media_sync
from ..services.platforms import detect_platform, list_platforms
from ..models.schemas import ExtractRequest

router = APIRouter(tags=["extract"])


@router.get("/platforms", summary="List supported platforms")
async def get_platforms():
    return {"platforms": list_platforms()}


def _effective_timeout() -> int:
    s = get_settings()
    # On Vercel, respect maxDuration: hobby 10s -> use 9s, pro 30s -> use 25s.
    # We default to min(extract_timeout, 9) on Vercel to stay safe.
    if s.is_vercel:
        # If user set EXTRACT_TIMEOUT explicitly low, honor it; otherwise cap at 9-28
        return min(s.extract_timeout, 28) if s.extract_timeout <= 10 else 9
    return s.extract_timeout


@router.get("/extract", summary="Extract media metadata (GET, query param)")
@limiter.limit("30/minute")
async def extract_media_get(
    request: Request,
    url: str = Query(..., description="URL to extract", min_length=1, max_length=2048),
    format: Optional[str] = Query(None, description="yt-dlp format selector"),
    audio_only: bool = Query(False),
    playlist: bool = Query(False),
    subtitles: bool = Query(False),
    _: None = Depends(verify_api_key),
):
    clean = validate_url(url)
    settings = get_settings()

    opts_hash = f"{format}:{audio_only}:{playlist}:{subtitles}"
    if settings.enable_cache and not playlist:
        cached = await cache_get(clean, opts_hash)
        if cached:
            cached = dict(cached)
            cached["_cached"] = True
            return cached

    timeout = _effective_timeout()
    try:
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: extract_media_sync(
                    clean,
                    custom_format=format,
                    audio_only=audio_only,
                    playlist=playlist,
                    include_subtitles=subtitles,
                ),
            ),
            timeout=timeout,
        )
        if settings.enable_cache and not result.get("blocked") and not playlist:
            await cache_set(clean, result, ttl=settings.cache_ttl, opts_hash=opts_hash)
        return result
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Extraction timed out after {timeout}s (Vercel limit). "
                "Try again — the cache may have it — or use audio_only=true, or a shorter format. "
                "If YouTube, it is likely blocked on this Vercel IP; you will get oEmbed fallback on retry."
            ),
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Extraction failed: {str(e)}")


@router.post("/extract", summary="Extract media metadata (POST, JSON body)")
@limiter.limit("30/minute")
async def extract_media_post(
    request: Request,
    body: ExtractRequest,
    _: None = Depends(verify_api_key),
):
    clean = validate_url(body.url)
    settings = get_settings()
    opts_hash = f"{body.format}:{body.audio_only}:{body.playlist}:{body.subtitles}"
    if settings.enable_cache and not body.playlist:
        cached = await cache_get(clean, opts_hash)
        if cached:
            cached = dict(cached)
            cached["_cached"] = True
            return cached

    timeout = _effective_timeout()
    try:
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: extract_media_sync(
                    clean,
                    custom_format=body.format,
                    audio_only=body.audio_only,
                    playlist=body.playlist,
                    playlist_items=body.playlist_items,
                    include_subtitles=body.subtitles,
                ),
            ),
            timeout=timeout,
        )
        if settings.enable_cache and not result.get("blocked") and not body.playlist:
            await cache_set(clean, result, ttl=settings.cache_ttl, opts_hash=opts_hash)
        return result
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"Extraction timed out after {timeout}s (Vercel limit). Try again or simplify request.")
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Extraction failed: {str(e)}")


# Legacy compatibility: some clients POST to /api/extract with url in query but no body
@router.post("/extract-compat", include_in_schema=False)
async def extract_compat(request: Request, url: str = Query(...), _: None = Depends(verify_api_key)):
    return await extract_media_get(request, url=url)


@router.get("/detect", summary="Detect platform for URL")
async def detect(url: str = Query(...)):
    clean = validate_url(url)
    platform = detect_platform(clean)
    return {"url": clean, "platform": platform}
