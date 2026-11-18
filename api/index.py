"""
toolz-downloadz-api v6.0 — single-file FastAPI app for Vercel.
Platforms: TikTok + Instagram (+ YouTube through the toolz-ytapi microservice, v1 only).

What changed vs v5:
  * Downloads are served from the stored extraction record — no re-extraction on the
    hot path — and fail over (other candidate URLs -> one fresh extraction) instead of
    burning ~20s and returning 502.
  * Fully async I/O on one pooled httpx client (Redis, tikwm, CDN streaming).
  * yt-dlp is a bounded, time-capped fallback; it no longer runs on every TikTok request.
  * Redis-backed rate limiting + result cache, with an in-process L1 in front.
  * Hardening: constant-time auth, SSRF/scheme checks on every media hop, body-size caps,
    header validation, ffmpeg protocol whitelist, TLS verification always on, no leaked
    upstream internals, fail-closed legacy API in production.

Run locally:  uvicorn api.index:app --reload   (or: python api/index.py)
"""
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import time
import urllib.parse
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.datastructures import MutableHeaders

try:  # optional: Vercel injects env vars, .env is for local dev only
    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:  # pragma: no cover
    pass


# ----------------------------------------------------------------------------
# Config (env)
# ----------------------------------------------------------------------------
def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _int_env(name: str, default: int) -> int:
    """Env int that never crashes the lambda on a malformed value."""
    try:
        return int(_env(name) or default)
    except (ValueError, TypeError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except (ValueError, TypeError):
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    v = _env(name).lower()
    return default if not v else v in ("1", "true", "yes", "on")


VERSION = "6.0.0"
ON_VERCEL = bool(os.getenv("VERCEL"))
IS_PROD = _env("VERCEL_ENV") == "production"

API_SECRET_KEY = _env("API_SECRET_KEY")
ALLOW_OPEN_LEGACY = _bool_env("ALLOW_OPEN_LEGACY")      # legacy routes without a key, even in production
INSTAGRAM_COOKIES = _env("INSTAGRAM_COOKIES")
YOUTUBE_COOKIES = _env("YOUTUBE_COOKIES")
YT_EXTRACT_URL = _env("YT_EXTRACT_URL").rstrip("/")
YTAPI_SECRET = _env("YTAPI_SECRET")

EXTRACT_TIMEOUT = _int_env("EXTRACT_TIMEOUT", 26)        # seconds; fits maxDuration=60
CACHE_TTL = _int_env("CACHE_TTL", 900)                   # shared extraction-result cache
RATE_LIMIT = _int_env("RATE_LIMIT", 30)                  # extractions per minute
SESSION_RATE = _int_env("SESSION_RATE", 10)              # guest sessions per minute per IP
DOWNLOAD_RATE = _int_env("DOWNLOAD_RATE", 90)            # downloads per minute per session (per instance)
GUEST_SESSION_TTL = _int_env("GUEST_SESSION_TTL", 60 * 60 * 24 * 14)
EXTRACTION_TTL = _int_env("EXTRACTION_TTL", 60 * 10)
YTDLP_TIMEOUT = _int_env("YTDLP_TIMEOUT", 14)            # per yt-dlp call (fallback engine)
DOWNLOAD_OPEN_BUDGET = _int_env("DOWNLOAD_OPEN_BUDGET", 40)  # total seconds spent finding a working media URL
TIKWM_MIN_INTERVAL = _float_env("TIKWM_MIN_INTERVAL", 1.0)   # tikwm's free API allows ~1 req/s per IP
TIKTOK_FULL_LADDER = _bool_env("TIKTOK_FULL_LADDER", False)  # opt-in: slow exact-quality yt-dlp ladder
MAX_BODY = 8 * 1024
YT_BUDGET = 50
YT_TICKET_TTL = 120
YT_FALLBACK_AFTER = 25
_YT_VERDICTS = ("private", "age", "region", "unavailable")

DOCS_ENABLED = _bool_env("ENABLE_DOCS", default=not IS_PROD)
CORS_ORIGINS = [o.strip() for o in (_env("CORS_ORIGINS") or "*").split(",") if o.strip()]

REDIS_REST_URL = (_env("UPSTASH_REDIS_REST_URL") or _env("KV_REST_API_URL")).rstrip("/")
REDIS_REST_TOKEN = _env("UPSTASH_REDIS_REST_TOKEN") or _env("KV_REST_API_TOKEN")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
BASE_HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

SUPPORTED = ["tiktok", "instagram"]
TIKWM_BASE = "https://www.tikwm.com"

GENERIC_FAIL = "We couldn't process this link right now. Please try again in a moment."


def log(event: str, **fields: Any) -> None:
    """One JSON line per event — Vercel log drains / alerts can key on `event`."""
    try:
        print(json.dumps({"event": event, **fields}, default=str, separators=(",", ":")), flush=True)
    except Exception:  # logging must never break a request
        pass


class UserFacingError(RuntimeError):
    """Message is safe to show to API clients. Anything else is logged and replaced."""


class UnsupportedMedia(UserFacingError):
    """Valid URL, but the content isn't downloadable (e.g. a photo slideshow)."""


# ----------------------------------------------------------------------------
# One pooled async HTTP client (keep-alive to Upstash / tikwm / CDNs)
# ----------------------------------------------------------------------------
_http: Optional[httpx.AsyncClient] = None
_http_loop: Optional[asyncio.AbstractEventLoop] = None


def http() -> httpx.AsyncClient:
    """Lazy per-event-loop client. Created lazily (not in a lifespan hook) because
    Vercel's ASGI adapter does not reliably run startup/shutdown events."""
    global _http, _http_loop
    loop = asyncio.get_running_loop()
    if _http is None or _http_loop is not loop:
        _http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=4.0, read=15.0, write=10.0, pool=5.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16, keepalive_expiry=30.0),
            follow_redirects=False,          # every hop is validated by hand (see open_upstream)
            headers=BASE_HEADERS,
        )
        _http_loop = loop
    return _http


# ----------------------------------------------------------------------------
# State: in-process L1 in front of Upstash Redis (REST). Redis down != outage.
# ----------------------------------------------------------------------------
_L1: Dict[str, Tuple[float, Any]] = {}
_L1_MAX = 2000
_redis_fails = 0
_redis_dead_until = 0.0
REDIS_TIMEOUT = httpx.Timeout(connect=1.5, read=2.5, write=2.5, pool=2.0)


def _l1_put(key: str, value: Any, ttl: float) -> None:
    if len(_L1) >= _L1_MAX:
        now = time.time()
        for k in [k for k, (exp, _) in _L1.items() if exp <= now]:
            _L1.pop(k, None)
        if len(_L1) >= _L1_MAX:  # still full: drop the oldest quarter
            for k, _ in sorted(_L1.items(), key=lambda kv: kv[1][0])[: _L1_MAX // 4]:
                _L1.pop(k, None)
    _L1[key] = (time.time() + max(1.0, ttl), value)


def redis_enabled() -> bool:
    return bool(REDIS_REST_URL and REDIS_REST_TOKEN)


async def _redis(*commands: list) -> Optional[list]:
    """Pipeline of commands in ONE round trip. None = unavailable. A tiny circuit
    breaker skips Redis for 15s after repeated failures so a Redis incident adds
    no per-request latency."""
    global _redis_fails, _redis_dead_until
    if not redis_enabled() or time.monotonic() < _redis_dead_until:
        return None
    try:
        r = await http().post(
            REDIS_REST_URL + "/pipeline", json=list(commands),
            headers={"Authorization": f"Bearer {REDIS_REST_TOKEN}"}, timeout=REDIS_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        _redis_fails = 0
        return [(item.get("result") if isinstance(item, dict) else None) for item in data]
    except Exception as exc:
        _redis_fails += 1
        if _redis_fails >= 3:
            _redis_fails, _redis_dead_until = 0, time.monotonic() + 15
        log("redis_error", type=type(exc).__name__)
        return None


async def state_get(key: str) -> Optional[Any]:
    hit = _L1.get(key)
    if hit:
        if hit[0] > time.time():
            return hit[1]
        _L1.pop(key, None)
    res = await _redis(["GET", key])
    raw = res[0] if res else None
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    _l1_put(key, value, 60)
    return value


async def state_set(key: str, value: Any, ttl: int) -> None:
    _l1_put(key, value, ttl)
    await _redis(["SET", key, json.dumps(value, separators=(",", ":")), "EX", max(1, int(ttl))])


# ----------------------------------------------------------------------------
# Rate limiting: per-instance sliding window + shared fixed window in Redis
# ----------------------------------------------------------------------------
_hits: Dict[str, deque] = defaultdict(deque)


def _local_rate_ok(ident: str, limit: int) -> bool:
    now = time.time()
    if len(_hits) > 5000:
        for k in [k for k, q in _hits.items() if not q or now - q[-1] > 60]:
            _hits.pop(k, None)
    q = _hits[ident]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


async def rate_ok(ident: str, limit: Optional[int] = None, shared: bool = True) -> bool:
    limit = limit or RATE_LIMIT
    if not _local_rate_ok(ident, limit):
        return False
    if shared and redis_enabled():
        key = f"downloadz:rl:{ident}:{int(time.time() // 60)}"
        res = await _redis(["INCR", key], ["EXPIRE", key, 90])
        if res and isinstance(res[0], int) and res[0] > limit:
            return False
    return True


# ----------------------------------------------------------------------------
# Cookies (env content -> /tmp file for yt-dlp)
# ----------------------------------------------------------------------------
_cookie_hash: Dict[str, str] = {}


def _cookies_file(content: str, name: str) -> Optional[str]:
    if not content:
        return None
    p = os.path.join("/tmp", name)
    digest = hashlib.sha256(content.encode()).hexdigest()
    if _cookie_hash.get(name) == digest and os.path.isfile(p):
        return p
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # cookies are secrets
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content if content.endswith("\n") else content + "\n")
        _cookie_hash[name] = digest
        return p
    except Exception:
        return None


# ----------------------------------------------------------------------------
# URL validation + platform detection — strict registered-domain allowlist
# ----------------------------------------------------------------------------
def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _source_host(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def detect_platform(url: str) -> Optional[str]:
    host = _source_host(url)
    if _host_is(host, "tiktok.com"):
        return "tiktok"
    path = urllib.parse.urlparse(url).path.lower()
    if _host_is(host, "instagram.com") and (path.startswith("/reel") or path.startswith("/p/")):
        return "instagram"
    return None


def detect_v1_platform(url: str) -> Optional[str]:
    legacy = detect_platform(url)
    if legacy:
        return legacy
    host = _source_host(url)
    if _host_is(host, "youtube.com") or _host_is(host, "youtube-nocookie.com") or host == "youtu.be":
        return "youtube"
    return None


def tiktok_canonical(url: str) -> str:
    return url.split("?")[0].split("#")[0]


def _youtube_id(url: str) -> Optional[str]:
    """11-char video id from watch?v=, youtu.be/, shorts/, embed/, live/."""
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if host == "youtu.be":
        seg = (u.path or "").strip("/").split("/")[0]
        return seg if len(seg) == 11 else None
    m = re.search(r"/(?:shorts|embed|live|v)/([a-zA-Z0-9_-]{11})", u.path or "")
    if m:
        return m.group(1)
    for k, v in urllib.parse.parse_qsl(u.query or ""):
        if k == "v" and len(v) == 11:
            return v
    return None


def _key_url(url: str) -> str:
    """Cache identity. TikTok/Instagram: drop tracking params. YouTube: the video id
    LIVES in the query string, so v5's "strip the query" collapsed every YouTube video
    onto one cache key — here it is canonicalised on the id instead."""
    if detect_v1_platform(url) == "youtube":
        vid = _youtube_id(url)
        return f"https://www.youtube.com/watch?v={vid}" if vid else url
    return url.split("?")[0].split("#")[0]


def _ckey(url: str, opts: str) -> str:
    return hashlib.sha256(f"{url}|{opts}".encode()).hexdigest()


def clean_url(url: str) -> str:
    url = (url or "").strip()
    if not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="URL must start with https://")
    if len(url) > 2048 or any(c.isspace() or ord(c) < 32 for c in url):
        raise HTTPException(status_code=400, detail="Invalid URL")
    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid URL")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or parsed.username or parsed.password or port not in (None, 443):
        raise HTTPException(status_code=400, detail="Invalid source URL")
    if _is_private_host(host):
        raise HTTPException(status_code=400, detail="Private/internal hosts are not allowed")
    return url


def _is_private_host(host: str) -> bool:
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return True
    try:
        return not ipaddress.ip_address(host.strip("[]")).is_global
    except ValueError:
        return False


def media_url_ok(url: str) -> bool:
    """Defence in depth for every upstream media hop (initial URL and each redirect):
    https only, a real hostname, never a private/loopback/link-local address. urllib
    (v5) would happily open file:// URLs handed back by a compromised upstream."""
    try:
        u = urllib.parse.urlparse(url)
        host = (u.hostname or "").lower().rstrip(".")
        return u.scheme == "https" and bool(host) and not u.username and not _is_private_host(host)
    except ValueError:
        return False


# ----------------------------------------------------------------------------
# oEmbed fallbacks (public endpoints)
# ----------------------------------------------------------------------------
async def _oembed(endpoint_url: str) -> Optional[dict]:
    try:
        r = await http().get(endpoint_url, timeout=httpx.Timeout(connect=3, read=5, write=3, pool=3))
        if r.status_code != 200:
            return None
        data = r.json()
        return {"title": data.get("title"), "uploader": data.get("author_name"),
                "uploader_url": data.get("author_url"), "thumbnail": data.get("thumbnail_url")}
    except Exception:
        return None


async def tiktok_oembed(url: str) -> Optional[dict]:
    return await _oembed("https://www.tiktok.com/oembed?url=" + urllib.parse.quote(tiktok_canonical(url), safe=""))


async def youtube_oembed(url: str) -> Optional[dict]:
    vid = _youtube_id(url)
    if not vid:
        return None
    return await _oembed("https://www.youtube.com/oembed?url=" +
                         urllib.parse.quote(f"https://www.youtube.com/watch?v={vid}", safe="") + "&format=json")


# ----------------------------------------------------------------------------
# TikTok engine #1: tikwm (fast, no-watermark, IP-free CDN)
# ----------------------------------------------------------------------------
def _abs(u: Any) -> Optional[str]:
    """tikwm sometimes returns site-relative paths ('/video/media/hdplay/…'); make every
    URL absolute https (or None)."""
    if not isinstance(u, str) or not u.strip():
        return None
    u = u.strip()
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return TIKWM_BASE + u
    if u.startswith("http://"):
        return "https://" + u[7:]
    return u if u.startswith("https://") else None


def _num(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError):
        return None


_tikwm_lock: Optional[asyncio.Lock] = None
_tikwm_last = 0.0


async def _tikwm_gate() -> None:
    """Respect tikwm's ~1 req/s free-tier limit from this instance instead of firing
    parallel calls that are guaranteed to be rejected."""
    global _tikwm_lock, _tikwm_last
    if _tikwm_lock is None:
        _tikwm_lock = asyncio.Lock()
    async with _tikwm_lock:
        wait = TIKWM_MIN_INTERVAL - (time.monotonic() - _tikwm_last)
        if wait > 0:
            await asyncio.sleep(wait)
        _tikwm_last = time.monotonic()


async def tikwm_fetch(url: str) -> Optional[dict]:
    """Raw tikwm `data` dict, or None. Retries transport errors and rate-limit replies
    with short async backoff (never blocks the event loop)."""
    api = TIKWM_BASE + "/api/?hd=1&url=" + urllib.parse.quote(tiktok_canonical(url), safe="")
    timeout = httpx.Timeout(connect=3.0, read=8.0, write=5.0, pool=3.0)
    started = time.monotonic()
    for attempt in range(3):
        if attempt and time.monotonic() - started > 14:
            break
        await _tikwm_gate()
        try:
            r = await http().get(api, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPError(f"status {r.status_code}")
            payload = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            log("tikwm_transport_error", attempt=attempt, type=type(exc).__name__)
            await asyncio.sleep(0.4 * (attempt + 1))
            continue
        if isinstance(payload, dict) and payload.get("code") == 0 and payload.get("data"):
            return payload["data"]
        msg = str((payload or {}).get("msg") or "").lower() if isinstance(payload, dict) else ""
        if "limit" in msg and attempt < 2:
            log("tikwm_rate_limited", attempt=attempt)
            await asyncio.sleep(1.2 + 0.4 * attempt)
            continue
        log("tikwm_rejected", msg=msg[:80])
        return None
    return None


def _row(format_id: str, ext: str, resolution: str, url: str, *, filesize: Optional[int] = None,
         vcodec: Optional[str] = None, has_audio: bool = True) -> dict:
    return {"format_id": format_id, "ext": ext, "resolution": resolution, "url": url,
            "filesize": filesize, "vcodec": vcodec, "acodec": None, "has_audio": has_audio,
            "ladder": "fast", "ip_free": True, "height": None, "tbr": None, "abr": None,
            "headers": dict(BASE_HEADERS), "cookies": None}


def _tikwm_meta(d: dict) -> dict:
    author = d.get("author") or {}
    ct = _num(d.get("create_time"))
    uid = author.get("unique_id")
    return {
        "platform": "tiktok",
        "title": d.get("title"),
        "thumbnail": _abs(d.get("cover")) or _abs(d.get("origin_cover")),
        "duration": _num(d.get("duration")),
        "uploader": author.get("nickname"),
        "uploader_url": f"https://www.tiktok.com/@{urllib.parse.quote(str(uid))}" if uid else None,
        "stats": {"view_count": _num(d.get("play_count")), "like_count": _num(d.get("digg_count")),
                  "comment_count": _num(d.get("comment_count"))},
        "upload_date": time.strftime("%Y%m%d", time.gmtime(ct)) if ct else None,
        "description": None,
    }


def _tikwm_shape(d: dict, url: str, audio_only: bool = False) -> Optional[dict]:
    """
    Build our response from a parsed tikwm payload. Pure (no I/O), so unit-testable.

    Honesty rules: only label what we observe. tikwm reports byte counts and duration but
    no dimensions/codecs, so the resolution tier carries a ~ prefix and codecs stay null.
    If tikwm only offers the WATERMARKED file we say so instead of calling it clean.
    """
    hd, sd, wm = _abs(d.get("hdplay")), _abs(d.get("play")), _abs(d.get("wmplay"))
    media = hd or sd or wm
    if not media:
        return None
    if hd:
        fid, label, size = "tikwm_hd", "~1080p HD, no watermark", _num(d.get("hd_size"))
    elif sd:
        fid, label, size = "tikwm_sd", "~SD, no watermark", _num(d.get("size"))
    else:
        fid, label, size = "tikwm_wm", "~SD, watermarked", _num(d.get("wm_size"))
    video = [_row(fid, "mp4", label, media, filesize=size)]
    if hd and sd and sd != hd:
        video.append(_row("tikwm_sd", "mp4", "~SD, no watermark", sd, filesize=_num(d.get("size"))))
    music = _abs(d.get("music"))
    audio = [_row("tikwm_music", "mp3", "audio", music, vcodec="none", has_audio=False)] if music else []

    out = _tikwm_meta(d)
    out.update({"blocked": False, "source": "tikwm", "ladder": "fast", "original_url": url,
                "download_cookies": None})
    if audio_only and audio:
        out.update({"download_url": audio[0]["url"], "download_headers": dict(BASE_HEADERS), "ext": "mp3",
                    "formats": {"video": [], "audio": audio}})
    else:
        out.update({"download_url": media, "download_headers": dict(BASE_HEADERS), "ext": "mp4",
                    "formats": {"video": video, "audio": audio}})
    return out


def _safe_ext(url: str, default: str = "jpg") -> str:
    ext = urllib.parse.urlparse(url).path.rsplit(".", 1)[-1].lower() if "." in urllib.parse.urlparse(url).path else ""
    return ext if re.fullmatch(r"[a-z0-9]{2,5}", ext) else default


def tiktok_gallery_shape(data: dict, url: str) -> Optional[dict]:
    images = [u for u in (_abs(i) for i in (data.get("images") or []) if isinstance(i, str)) if u]
    if not images:
        return None
    music = _abs(data.get("music"))
    audio = [_row("tikwm_music", "mp3", "audio", music, vcodec="none", has_audio=False)] if music else []
    gallery = [{
        "format_id": f"gallery:{i}", "url": img, "headers": dict(BASE_HEADERS), "cookies": None,
        "ext": _safe_ext(img), "filesize": None, "width": None, "height": None,
        "thumbnail": img, "mime_type": None,
    } for i, img in enumerate(images)]
    out = _tikwm_meta(data)
    out.update({
        "thumbnail": out["thumbnail"] or gallery[0]["thumbnail"], "duration": None, "uploader_url": None,
        "upload_date": None, "download_url": gallery[0]["url"], "download_headers": dict(BASE_HEADERS),
        "download_cookies": None, "ext": gallery[0]["ext"], "blocked": False,
        "formats": {"video": [], "audio": audio}, "gallery": gallery, "original_url": url,
    })
    return out


# ----------------------------------------------------------------------------
# yt-dlp (fallback engine): bounded pool, hard time caps, TLS verification ON
# ----------------------------------------------------------------------------
_YTDLP_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ydl")


def ydl_opts(platform: str, audio_only: bool = False, custom_format: Optional[str] = None) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 8,
        "retries": 1,
        "extractor_retries": 1,
        "cachedir": False,                # Vercel's filesystem is read-only outside /tmp
        "geo_bypass": True,
        "http_headers": BASE_HEADERS,
        "extractor_args": {},
    }
    if platform == "tiktok":
        opts["format"] = custom_format or ("bestaudio/best" if audio_only else "best")
    elif platform == "instagram":
        cf = _cookies_file(INSTAGRAM_COOKIES, "ig_cookies.txt")
        if cf:
            opts["cookiefile"] = cf
        opts["format"] = custom_format or ("bestaudio/best" if audio_only else "best")
    elif platform == "youtube":
        # yt-dlp is only the FALLBACK for YouTube (toolz-ytapi is primary). Recent yt-dlp
        # needs an external JS runtime for YouTube; on Vercel only the clients below,
        # which serve plain URLs, can work at all.
        cf = _cookies_file(YOUTUBE_COOKIES, "yt_cookies.txt")
        if cf:
            opts["cookiefile"] = cf
        opts["format"] = custom_format or ("bestaudio/best" if audio_only else "best/bestvideo+bestaudio")
        opts["extractor_args"] = {"youtube": {"player_client": ["android_vr", "tv", "mweb"]}}
    return opts


def _run_ydl(opts: Dict[str, Any], url: str) -> dict:
    import yt_dlp  # imported lazily: the tikwm fast path never pays this cold-start cost
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


async def run_ydl(opts: Dict[str, Any], url: str, timeout: float) -> dict:
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.run_in_executor(_YTDLP_POOL, _run_ydl, opts, url), timeout)


# ----------------------------------------------------------------------------
# Response shaping (pure)
# ----------------------------------------------------------------------------
def normalize(info: dict) -> Tuple[List[dict], List[dict]]:
    video, audio = [], []
    for f in info.get("formats") or []:
        if not f.get("url"):
            continue
        proto = f.get("protocol") or ""
        if "m3u8" in proto or f.get("ext") == "m3u8" or ".m3u8" in f["url"]:
            continue  # HLS manifests are playlists, not files
        if f.get("fragments"):
            continue  # DASH segment lists can't be served as a single download
        note = f"{f.get('format_note') or ''} {f.get('format_id') or ''}".lower()
        if "watermark" in note:
            continue
        fmt = {
            "format_id": f.get("format_id"), "ext": f.get("ext"),
            "resolution": f.get("resolution") or f.get("format_note") or "unknown",
            "url": f["url"], "filesize": f.get("filesize"),
            "vcodec": f.get("vcodec"), "acodec": f.get("acodec"), "has_audio": f.get("has_audio"),
            "height": f.get("height"), "width": f.get("width"),
            "tbr": f.get("tbr"), "abr": f.get("abr"), "fps": f.get("fps"),
            "headers": dict(f.get("http_headers") or {}), "cookies": f.get("cookies"),
        }
        (audio if f.get("vcodec") == "none" else video).append(fmt)

    video.sort(key=lambda x: (x.get("height") or 0, x.get("tbr") or 0, x.get("filesize") or 0), reverse=True)
    audio.sort(key=lambda x: (x.get("abr") or x.get("tbr") or 0, x.get("filesize") or 0), reverse=True)
    return _dedupe(video, ("width", "height", "vcodec", "acodec")), _dedupe(audio, ("acodec", "abr"))


def _dedupe(rows: List[dict], keys: Tuple[str, ...]) -> List[dict]:
    """Collapse same-spec rows. Keeps the entry with a known filesize, else highest bitrate."""
    best: Dict[tuple, dict] = {}

    def score(r: dict) -> tuple:
        return (1 if r.get("filesize") else 0, r.get("tbr") or r.get("abr") or 0)

    for r in rows:
        k = tuple(r.get(c) for c in keys)
        if k not in best or score(r) > score(best[k]):
            best[k] = r
    winners = {id(r) for r in best.values()}
    return [r for r in rows if id(r) in winners]  # O(n), keeps original ordering


def prefer_1080(rows: List[dict]) -> List[dict]:
    exact = [r for r in rows if (r.get("height") or 0) == 1080]
    return (exact + [r for r in rows if r not in exact]) if exact else rows


def shape(platform: str, info: dict, original_url: str) -> dict:
    video, audio = normalize(info)
    dl = info.get("url")
    hdrs = dict(info.get("http_headers") or {})
    dl_cookies = None
    if not dl and info.get("requested_formats"):
        req = info["requested_formats"]
        is_split_pair = len(req) > 1 and any(f.get("vcodec") == "none" or f.get("acodec") == "none" for f in req)
        if not is_split_pair:
            wanted = [f for f in req
                      if f.get("vcodec") != "none" and "m3u8" not in (f.get("protocol") or "")
                      and ".m3u8" not in (f.get("url") or "")] or req
            pick = wanted[0]
            dl, hdrs, dl_cookies = pick.get("url"), dict(pick.get("http_headers") or {}), pick.get("cookies")
    if not dl:
        merged = [f for f in video if f["vcodec"] not in (None, "none") and f["acodec"] not in (None, "none")]
        pick = prefer_1080(merged or video or audio)
        if pick:
            dl, hdrs = pick[0]["url"], dict(pick[0]["headers"] or {})
    if dl and not dl_cookies:
        for f_ in info.get("formats") or []:
            if f_.get("url") == dl and f_.get("cookies"):
                dl_cookies = f_["cookies"]
                break
    return {
        "platform": platform, "title": info.get("title"), "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"), "uploader": info.get("uploader"),
        "uploader_url": info.get("uploader_url"),
        "stats": {k: info.get(k) for k in ("view_count", "like_count", "comment_count")},
        "upload_date": info.get("upload_date"),
        "description": (info.get("description") or "")[:400] or None,
        "download_url": dl, "download_headers": hdrs, "download_cookies": dl_cookies,
        "ext": info.get("ext"), "blocked": False,
        "formats": {"video": video[:20], "audio": audio[:10]}, "original_url": original_url,
    }


def gallery_shape(platform: str, info: dict, original_url: str) -> Optional[dict]:
    """Normalize playlist/carousel entries into ordered, individually downloadable assets."""
    entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)]
    if not entries:
        return None
    gallery = []
    for index, entry in enumerate(entries):
        media = entry.get("url")
        headers = dict(entry.get("http_headers") or {})
        cookies = entry.get("cookies")
        if not media:
            video, audio = normalize(entry)
            choice = (video or audio or [None])[0]
            if choice:
                media, headers, cookies = choice["url"], dict(choice.get("headers") or {}), choice.get("cookies")
        if not media:
            continue
        ext = entry.get("ext") or _safe_ext(media)
        gallery.append({
            "format_id": f"gallery:{index}", "url": media, "headers": headers, "cookies": cookies,
            "ext": str(ext).lower(), "filesize": entry.get("filesize"), "width": entry.get("width"),
            "height": entry.get("height"), "thumbnail": entry.get("thumbnail") or media,
            "mime_type": entry.get("mime_type"),
        })
    if not gallery:
        return None
    first = gallery[0]
    return {
        "platform": platform, "title": info.get("title"), "thumbnail": info.get("thumbnail") or first["thumbnail"],
        "duration": None, "uploader": info.get("uploader"), "uploader_url": info.get("uploader_url"),
        "stats": {k: info.get(k) for k in ("view_count", "like_count", "comment_count")},
        "upload_date": info.get("upload_date"), "description": (info.get("description") or "")[:400] or None,
        "download_url": first["url"], "download_headers": first["headers"], "download_cookies": first["cookies"],
        "ext": first["ext"], "blocked": False, "formats": {"video": [], "audio": []},
        "gallery": gallery, "original_url": original_url,
    }


def blocked_shape(platform: str, meta: dict, original_url: str, msg: str) -> dict:
    return {
        "platform": platform, "title": meta.get("title"), "thumbnail": meta.get("thumbnail"),
        "duration": None, "uploader": meta.get("uploader"), "uploader_url": meta.get("uploader_url"),
        "stats": {}, "upload_date": None, "description": None,
        "download_url": None, "download_headers": {}, "ext": None, "blocked": True, "blocked_message": msg,
        "formats": {"video": [], "audio": []}, "original_url": original_url,
    }


BLOCK_MSGS = {"tiktok": "TikTok extraction failed on this server. Metadata below is from TikTok's public oEmbed. "
                        "Try again in a minute."}


# ----------------------------------------------------------------------------
# Extraction orchestration
# ----------------------------------------------------------------------------
async def tiktok_ladder(url: str, audio_only: bool, fast: dict) -> dict:
    """Opt-in (TIKTOK_FULL_LADDER=1): merge exact yt-dlp rungs into the tikwm result.
    Capped at 8s and degrades silently to the fast result."""
    out = dict(fast)
    try:
        info = await run_ydl(ydl_opts("tiktok", audio_only), tiktok_canonical(url), min(YTDLP_TIMEOUT, 8))
    except Exception as e:
        out["ladder_error"] = str(e).replace("ERROR: ", "")[:160]
        return out
    video, audio = normalize(info)
    for r in video + audio:
        r["ladder"], r["ip_free"] = "full", False
    fast_audio = fast.get("formats", {}).get("audio", [])
    out["formats"] = {"video": video[:20] or fast.get("formats", {}).get("video", []),
                      "audio": (fast_audio or audio)[:10]}
    out["ladder"] = "full" if video else "fast"
    if not video:
        out["ladder_error"] = "Full ladder unavailable — showing fast qualities."
    return out


async def extract_tiktok(url: str, audio_only: bool, custom_format: Optional[str]) -> dict:
    raw = await tikwm_fetch(url)
    if raw is not None:
        if raw.get("images") and not (raw.get("hdplay") or raw.get("play") or raw.get("wmplay")):
            gallery = tiktok_gallery_shape(raw, url)
            if gallery:
                return gallery
            raise UnsupportedMedia("This TikTok photo post did not expose downloadable images.")
        shaped = _tikwm_shape(raw, url, audio_only)
        if shaped:
            if TIKTOK_FULL_LADDER and not audio_only:
                return await tiktok_ladder(url, audio_only, shaped)
            return shaped
    # Fallback engine. TikTok often blocks datacenter IPs, so this is capped hard.
    started, last = time.monotonic(), None
    for candidate in dict.fromkeys([url, tiktok_canonical(url)]):
        if time.monotonic() - started > YTDLP_TIMEOUT:
            break
        try:
            info = await run_ydl(ydl_opts("tiktok", audio_only, custom_format), candidate, YTDLP_TIMEOUT)
            return shape("tiktok", info, url)
        except Exception as e:
            last = e
    meta = await tiktok_oembed(url)
    log("tiktok_extract_failed", err=str(last)[:200] if last else "tikwm-unavailable")
    if meta:
        return blocked_shape("tiktok", meta, url, BLOCK_MSGS["tiktok"])
    raise UserFacingError("TikTok didn't return this video. It may be private, removed, or temporarily unreachable.")


async def extract_instagram(url: str, audio_only: bool, custom_format: Optional[str]) -> dict:
    try:
        info = await run_ydl(ydl_opts("instagram", audio_only, custom_format), url, max(YTDLP_TIMEOUT, 20))
        return gallery_shape("instagram", info, url) or shape("instagram", info, url)
    except Exception as e:
        # The operator-facing hint (cookies) goes to the log, not to anonymous clients.
        log("instagram_extract_failed", err=str(e).replace("ERROR: ", "")[:250],
            cookies_configured=bool(INSTAGRAM_COOKIES))
        raise UserFacingError("Instagram didn't return this post. It may be private or require sign-in.")


async def extract_legacy(url: str, audio_only: bool = False, custom_format: Optional[str] = None) -> dict:
    platform = detect_platform(url)
    if platform == "tiktok":
        return await extract_tiktok(url, audio_only, custom_format)
    if platform == "instagram":
        return await extract_instagram(url, audio_only, custom_format)
    raise ValueError("Unsupported URL. Only TikTok and Instagram are supported.")


# ---- YouTube (toolz-ytapi microservice, contract v2) -----------------------
async def _ytapi_fetch(url: str) -> dict:
    """Raises RuntimeError on any failure."""
    r = None
    try:
        r = await http().post(
            YT_EXTRACT_URL + "/api/extract", json={"url": url, "v": 2},
            headers={"Authorization": f"Bearer {YTAPI_SECRET}"},
            timeout=httpx.Timeout(connect=4.0, read=32.0, write=5.0, pool=5.0),
        )
        body = r.json()
    except httpx.HTTPError as e:
        raise RuntimeError(f"extraction service unreachable: {type(e).__name__}")
    except ValueError:
        raise RuntimeError(f"extraction service unreachable: HTTP {r.status_code if r is not None else '?'}")
    if not isinstance(body, dict) or not body.get("ok"):
        body = body if isinstance(body, dict) else {}
        raise RuntimeError(f"extraction service: {body.get('error', 'failed')}: {str(body.get('detail') or '')[:900]}")
    data = body.get("data") or {}
    if not data.get("formats"):
        raise RuntimeError("extraction service returned no streams")
    return data


def youtube_shape(contract: dict, url: str, audio_only: bool = False) -> dict:
    """Build our response from the microservice contract (v2). Pure (no I/O).

    The `url` on every row is an opaque sentinel (`ytapi://<id>/<format>`): googlevideo URLs
    are bound to the microservice's egress IP, so this API never holds or fetches them.
    """
    vid = contract.get("video_id") or _youtube_id(url) or ""
    video: List[dict] = []
    audio: List[dict] = []
    for r in contract.get("formats") or []:
        if not isinstance(r, dict) or not r.get("format_id"):
            continue
        fid = str(r["format_id"])
        mime = str(r.get("mime") or "")
        is_audio = bool(r.get("is_audio"))
        ext = r.get("ext") or mime.split(";")[0].split("/")[-1].lower() or "mp4"
        if is_audio and ext == "mp4":
            ext = "m4a"
        codec = None
        m = re.search(r'codecs="([^"]+)"', mime)
        if m:
            codec = m.group(1).split(",")[0].strip().split(".")[0] or None
        bitrate = r.get("bitrate") or 0
        row = {
            "format_id": fid, "ext": ext,
            "resolution": r.get("quality_label") or ("audio" if is_audio else "unknown"),
            "url": f"ytapi://{vid}/{fid}", "filesize": r.get("content_length"),
            "vcodec": "none" if is_audio else (None if r.get("kind") == "merged" else codec),
            "acodec": codec if is_audio else None,
            "has_audio": False if is_audio else bool(r.get("has_audio")),
            "height": r.get("height"), "width": r.get("width"),
            "tbr": (bitrate / 1000) if bitrate else None,
            "abr": (bitrate / 1000) if (is_audio and bitrate) else None,
            "fps": r.get("fps"), "mime_type": mime.split(";")[0].strip() or None,
            "headers": {}, "cookies": None,
        }
        (audio if is_audio else video).append(row)
    if not video and not audio:
        raise RuntimeError("extraction service returned no streams")

    sounded = {v["height"] for v in video if v["has_audio"]}
    video = [v for v in video if v["has_audio"] or v["height"] not in sounded]
    video.sort(key=lambda v: (v["height"] or 0, v["fps"] or 0, 1 if v["has_audio"] else 0, v["tbr"] or 0), reverse=True)
    audio.sort(key=lambda a: (a["abr"] or 0, 1 if a["ext"] == "m4a" else 0), reverse=True)

    with_sound = [v for v in video if v["has_audio"]]
    if audio_only and audio:
        best, best_group = audio[0], "audio"
    elif video:
        best, best_group = prefer_1080(with_sound or video)[0], "video"
    else:
        best, best_group = audio[0], "audio"

    return {
        "platform": "youtube", "source": "ytapi", "video_id": vid,
        "title": contract.get("title"), "thumbnail": contract.get("thumbnail"),
        "duration": contract.get("duration"), "uploader": contract.get("uploader"),
        "uploader_url": contract.get("uploader_url"),
        "stats": {"view_count": contract.get("view_count"), "like_count": contract.get("like_count"),
                  "comment_count": contract.get("comment_count")},
        "upload_date": contract.get("upload_date"),
        "description": (contract.get("description") or "")[:400] or None,
        "download_url": None, "download_headers": {}, "download_cookies": None,
        "ext": best["ext"], "blocked": False, "best_format": best["format_id"], "best_group": best_group,
        "formats": {"video": video[:20], "audio": audio[:10]}, "original_url": url,
    }


def _ytapi_fatal(msg: str) -> bool:
    m = re.search(r"extraction service:\s*(\w+):", msg or "")
    return bool(m and m.group(1) in _YT_VERDICTS)


def _ytapi_primary_error(ytapi_msg: str, ytdl_msg: str) -> UserFacingError:
    m = re.search(r"extraction service:\s*(\w+):\s*(.*)", ytapi_msg)
    if not m:
        log("youtube_extract_failed", ytapi=ytapi_msg[:200], ytdlp=ytdl_msg[:200])
        return UserFacingError("YouTube didn't return this video right now. Please try again shortly.")
    code, detail = m.group(1), m.group(2).strip()
    if code == "private":
        return UserFacingError("This YouTube video is private.")
    if code == "age":
        return UserFacingError(f"This YouTube video is age-restricted ({detail[:120]}).")
    if code == "region":
        return UserFacingError(f"This YouTube video is blocked in this region ({detail[:120]}).")
    if code == "unavailable":
        return UserFacingError("This YouTube video doesn't exist, is private, or was removed. "
                               "Check the link and try a public video.")
    log("youtube_extract_failed", code=code, detail=detail[:300], ytdlp=ytdl_msg[:160])
    return UserFacingError("YouTube didn't return this video right now. Please try again shortly.")


def _ytapi_download_url(video_id: str, format_id: str, filename: str, height: Optional[int] = None) -> str:
    """Signed, short-lived link to the microservice's streaming endpoint
    (wire format matches toolz-ytapi/lib/ticket.js: b64url(JSON) "." b64url(HMAC-SHA256))."""
    if not (YT_EXTRACT_URL and YTAPI_SECRET):
        raise HTTPException(503, "YouTube downloads are not configured.")
    payload = {"v": video_id, "f": format_id, "n": filename, "e": int(time.time()) + YT_TICKET_TTL}
    if height:
        payload["h"] = int(height)
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(hmac.new(YTAPI_SECRET.encode(), body.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return f"{YT_EXTRACT_URL}/api/extract?t={body}.{sig}"


async def extract_youtube(url: str, audio_only: bool) -> dict:
    started = time.monotonic()
    ytapi_msg = ""
    if YT_EXTRACT_URL:
        try:
            return youtube_shape(await _ytapi_fetch(url), url, audio_only)
        except RuntimeError as e:
            ytapi_msg = str(e)
        except Exception as e:  # shaping bugs must not mask the fallback
            ytapi_msg = f"extraction service: failed: {str(e)[:160]}"
        if _ytapi_fatal(ytapi_msg):
            raise _ytapi_primary_error(ytapi_msg, "")
        if time.monotonic() - started > YT_FALLBACK_AFTER:
            raise _ytapi_primary_error(ytapi_msg, "skipped: time budget")
    try:
        info = await run_ydl(ydl_opts("youtube", audio_only), url, max(YTDLP_TIMEOUT, YT_BUDGET - (time.monotonic() - started) - 5))
        return gallery_shape("youtube", info, url) or shape("youtube", info, url)
    except Exception as exc:
        msg = str(exc).replace("ERROR: ", "")
        if ytapi_msg:
            raise _ytapi_primary_error(ytapi_msg, msg)
        if ("Requested format is not available" in msg or "no video formats" in msg.lower()) and not await youtube_oembed(url):
            raise UserFacingError("This YouTube video doesn't exist, is private, or was removed. "
                                  "Check the link and try a public video.")
        log("youtube_extract_failed", ytdlp=msg[:300])
        raise UserFacingError("YouTube didn't return this video right now. Please try again shortly.")


async def extract_v1(url: str, audio_only: bool = False) -> dict:
    if detect_v1_platform(url) == "youtube":
        return await extract_youtube(url, audio_only)
    return await extract_legacy(url, audio_only)


# ---- cache + single-flight --------------------------------------------------
_inflight: Dict[str, "asyncio.Future[dict]"] = {}


def _budget(url: str, v1: bool) -> float:
    return YT_BUDGET if (v1 and detect_v1_platform(url) == "youtube") else max(EXTRACT_TIMEOUT, 5)


async def _extract_and_cache(cache_key: str, url: str, audio_only: bool, custom_format: Optional[str], v1: bool) -> dict:
    coro = extract_v1(url, audio_only) if v1 else extract_legacy(url, audio_only, custom_format)
    result = await asyncio.wait_for(coro, timeout=YT_BUDGET + 5)  # hard stop for the shared task
    if not result.get("blocked"):
        await state_set(cache_key, result, CACHE_TTL)
    return result


def _done(key: str):
    def cb(task: "asyncio.Future[dict]") -> None:
        if _inflight.get(key) is task:
            _inflight.pop(key, None)
        if not task.cancelled():
            task.exception()  # mark retrieved so a waiter-less failure doesn't log "never retrieved"
    return cb


async def get_result(url: str, *, audio_only: bool = False, custom_format: Optional[str] = None,
                     v1: bool = False, fresh: bool = False, budget: Optional[float] = None) -> dict:
    """Cached, de-duplicated extraction. Identical concurrent requests share ONE upstream
    call; a waiter that times out does not cancel it, so the retry hits a warm cache."""
    key = "downloadz:v2:result:" + _ckey(_key_url(url), f"{v1}|{audio_only}|{custom_format}")
    if not fresh:
        cached = await state_get(key)
        if cached:
            return cached
    task = _inflight.get(key)
    if task is None:
        task = asyncio.ensure_future(_extract_and_cache(key, url, audio_only, custom_format, v1))
        _inflight[key] = task
        task.add_done_callback(_done(key))
    return await asyncio.wait_for(asyncio.shield(task), timeout=budget or _budget(url, v1))


# ----------------------------------------------------------------------------
# ffmpeg (optional: merges split tracks)
# ----------------------------------------------------------------------------
_FFMPEG: Optional[str] = ""  # "" = not looked up yet, None = unavailable


def ffmpeg_path() -> Optional[str]:
    global _FFMPEG
    if _FFMPEG != "":
        return _FFMPEG
    cand = _env("FFMPEG_PATH") or None
    if not cand:
        try:
            import imageio_ffmpeg  # type: ignore
            cand = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            cand = shutil.which("ffmpeg")
    _FFMPEG = cand if cand and os.path.isfile(cand) and os.access(cand, os.X_OK) else None
    return _FFMPEG


def _mux_pairs(result: dict) -> List[dict]:
    """Synthetic "video + audio" rows for yt-dlp sources that only offer split tracks at
    their top qualities. Only built when ffmpeg exists, only for heights with no with-sound
    row, one per (height, container), paired with the best same-family audio."""
    if result.get("source") == "ytapi" or result.get("gallery") or not ffmpeg_path():
        return []
    fm = result.get("formats") or {}
    video, audio = fm.get("video") or [], fm.get("audio") or []

    def ext(r: dict) -> str:
        return str(r.get("ext") or "").lower()

    pools = {
        "mp4": [a for a in audio if a.get("url") and ext(a) in ("m4a", "mp4", "aac")],
        "webm": [a for a in audio if a.get("url") and ext(a) in ("webm", "opus")],
    }
    sounded = {v.get("height") for v in video if v.get("has_audio") is True or v.get("acodec") not in (None, "none")}
    out: List[dict] = []
    seen = set()
    for v in video:
        if not v.get("url") or v.get("acodec") != "none" or not v.get("height") or v["height"] in sounded:
            continue
        cont = "webm" if ext(v) == "webm" else "mp4"
        if not pools[cont] or (v["height"], cont) in seen:
            continue
        seen.add((v["height"], cont))
        a = pools[cont][0]
        both = v.get("filesize") and a.get("filesize")
        out.append({
            "format_id": f"mux:{v['format_id']}+{a['format_id']}", "ext": cont,
            "resolution": v.get("resolution"), "url": v["url"],
            "filesize": (v["filesize"] + a["filesize"]) if both else None,
            "vcodec": v.get("vcodec"), "acodec": a.get("acodec"), "has_audio": True,
            "height": v["height"], "width": v.get("width"), "tbr": v.get("tbr"), "fps": v.get("fps"),
            "headers": {}, "cookies": None, "mux": True, "_video": v, "_audio": a,
        })
    return out


# ----------------------------------------------------------------------------
# Auth / sessions / body parsing
# ----------------------------------------------------------------------------
def client_ip(request: Request) -> str:
    """Real visitor IP. On Vercel the edge sets x-vercel-forwarded-for / x-real-ip itself;
    in v5 the *first* X-Forwarded-For entry was trusted, which any caller can spoof to
    dodge the rate limit. Off Vercel those headers are ignored unless TRUST_PROXY_HEADERS=1."""
    if ON_VERCEL or _bool_env("TRUST_PROXY_HEADERS"):
        for name in ("x-vercel-forwarded-for", "x-real-ip"):
            v = (request.headers.get(name) or "").split(",")[0].strip()
            if v:
                return v[:64]
    return request.client.host if request.client else "anon"


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def check_auth(request: Request, allow_query_key: bool = False) -> None:
    """Legacy-route auth (constant-time). Without API_SECRET_KEY the legacy routes are open
    in dev but FAIL CLOSED in production — they return raw upstream URLs and headers."""
    if not API_SECRET_KEY:
        if IS_PROD and not ALLOW_OPEN_LEGACY:
            raise HTTPException(status_code=503, detail="Legacy API is disabled. Use the v1 API.")
        return
    provided = (request.headers.get("x-api-key") or "").strip()
    if not provided:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
    if not provided and allow_query_key:  # query keys land in access logs: browser downloads only
        provided = (request.query_params.get("key") or "").strip()
    if not provided or not _eq(provided, API_SECRET_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing API key")


async def read_json(request: Request, limit: int = MAX_BODY) -> dict:
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > limit:
        raise HTTPException(status_code=413, detail="Request body too large")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(status_code=413, detail="Request body too large")
    if not buf:
        return {}
    try:
        data = json.loads(bytes(buf))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return data


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _guest_key(token: str) -> str:
    return "downloadz:v1:guest:" + _h(token)


def _extraction_key(extraction_id: str) -> str:
    return "downloadz:v1:extraction:" + extraction_id


EXTRACTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,64}$")


async def issue_guest_session(installation_id: str, ip: str) -> dict:
    installation_id = (installation_id or "").strip()
    if not (16 <= len(installation_id) <= 128) or not installation_id.isprintable():
        raise HTTPException(status_code=400, detail="installation_id must be 16–128 printable characters")
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    await state_set(_guest_key(token), {
        "installation_hash": _h(installation_id), "ip_hash": _h(ip)[:16],
        "created_at": now, "expires_at": now + GUEST_SESSION_TTL,
    }, GUEST_SESSION_TTL)
    return {"access_token": token, "token_type": "Bearer", "expires_in": GUEST_SESSION_TTL}


async def require_guest_session(request: Request) -> Tuple[str, dict]:
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="A guest session bearer token is required")
    token = auth[7:].strip()
    if not (32 <= len(token) <= 128):
        raise HTTPException(status_code=401, detail="Invalid guest session")
    session = await state_get(_guest_key(token))
    if not session or int(session.get("expires_at", 0)) <= time.time():
        raise HTTPException(status_code=401, detail="Guest session expired. Create a new session.")
    return token, session


async def load_record(extraction_id: str, session: dict, detail: str) -> dict:
    if not EXTRACTION_ID_RE.match(extraction_id or ""):
        raise HTTPException(status_code=404, detail=detail)
    record = await state_get(_extraction_key(extraction_id))
    if (not record or int(record.get("expires_at", 0)) <= time.time()
            or not _eq(str(record.get("owner") or ""), str(session.get("installation_hash") or ""))):
        raise HTTPException(status_code=404, detail=detail)
    return record


# ----------------------------------------------------------------------------
# v1 extraction records (opaque asset handles; upstream details never leave)
# ----------------------------------------------------------------------------
def _mime_type(ext: Optional[str], kind: str) -> str:
    e = (ext or "").lower()
    if e in {"jpg", "jpeg"}:
        return "image/jpeg"
    if e == "png":
        return "image/png"
    if e == "webp":
        return "image/webp"
    if e == "gif":
        return "image/gif"
    if e in {"mp3", "mpeg"}:
        return "audio/mpeg"
    if e in {"m4a", "mp4a"}:
        return "audio/mp4"
    if e in {"webm", "opus"} and kind == "audio":
        return "audio/webm"
    if e == "webm":
        return "video/webm"
    return "video/mp4" if kind == "video" else "application/octet-stream"


def _asset_record(asset_id: str, kind: str, fmt: dict, source_format: str) -> dict:
    return {
        "id": asset_id, "kind": kind, "format": source_format,
        "ext": fmt.get("ext") or ("jpg" if kind == "image" else "mp4"),
        "mime_type": fmt.get("mime_type") or _mime_type(fmt.get("ext"), kind),
        "filesize": fmt.get("filesize"), "width": fmt.get("width"), "height": fmt.get("height"),
        "duration": fmt.get("duration"), "resolution": fmt.get("resolution"),
        "vcodec": fmt.get("vcodec"), "acodec": fmt.get("acodec"),
        "has_audio": fmt.get("has_audio"), "thumbnail": fmt.get("thumbnail"),
    }


def build_v1_record(source_url: str, result: dict, owner_installation_hash: str) -> dict:
    extraction_id = secrets.token_urlsafe(18)
    asset_map: Dict[str, dict] = {}
    best = {"ext": result.get("ext"), "filesize": None, "duration": result.get("duration"),
            "has_audio": not bool(result.get("gallery")), "thumbnail": result.get("thumbnail")}
    best_kind, best_source = "video", "best"
    best_id = result.get("best_format")
    if best_id:
        for group in ("video", "audio"):
            row = next((f for f in result.get("formats", {}).get(group, []) if str(f.get("format_id")) == str(best_id)), None)
            if row:
                best.update({k: row.get(k) for k in ("ext", "filesize", "width", "height", "resolution",
                                                     "vcodec", "acodec", "mime_type")})
                best["has_audio"] = True if group == "video" and row.get("has_audio") else bool(row.get("has_audio"))
                best_kind, best_source = group, str(best_id)
                break
    asset_map["best"] = _asset_record("best", best_kind, best, best_source)
    for group, kind in (("video", "video"), ("audio", "audio")):
        for index, fmt in enumerate(result.get("formats", {}).get(group, [])):
            fmt_id = str(fmt.get("format_id") or "")
            if not fmt_id or not fmt.get("url"):
                continue
            asset_id = f"{kind[0]}_{index}_{hashlib.sha256(fmt_id.encode()).hexdigest()[:8]}"
            asset_map[asset_id] = _asset_record(asset_id, kind, fmt, fmt_id)
    for index, fmt in enumerate(_mux_pairs(result)):
        asset_id = f"m_{index}_{hashlib.sha256(fmt['format_id'].encode()).hexdigest()[:8]}"
        asset_map[asset_id] = _asset_record(asset_id, "video", fmt, fmt["format_id"])
    for index, item in enumerate(result.get("gallery", [])):
        if item.get("url"):
            asset_map[f"i_{index}"] = _asset_record(f"i_{index}", "image", item, str(item.get("format_id") or f"gallery:{index}"))
    if result.get("gallery"):
        asset_map.pop("best", None)  # a gallery has no meaningful generic "best" file
    now = int(time.time())
    return {"id": extraction_id, "owner": owner_installation_hash, "created_at": now,
            "expires_at": now + EXTRACTION_TTL, "source_url": source_url, "result": result, "assets": asset_map}


def public_v1_extraction(record: dict) -> dict:
    result, extraction_id = record["result"], record["id"]
    assets = []
    for asset in record["assets"].values():
        public = {k: v for k, v in asset.items() if k != "format"}
        public["download_path"] = f"/api/v1/extractions/{extraction_id}/assets/{asset['id']}/download"
        assets.append(public)
    kind_order = {"video": 0, "audio": 1, "image": 2}
    assets.sort(key=lambda item: (kind_order.get(item["kind"], 9), item["id"]))
    return {
        "id": extraction_id, "status": "ready", "expires_at": record["expires_at"],
        "platform": result.get("platform"), "title": result.get("title"), "thumbnail": result.get("thumbnail"),
        "duration": result.get("duration"), "uploader": result.get("uploader"), "uploader_url": result.get("uploader_url"),
        "stats": result.get("stats") or {}, "description": result.get("description"),
        "media_kind": "gallery" if result.get("gallery") else "media", "assets": assets,
    }


async def refresh_record(record: dict, result: dict) -> None:
    """Swap in a freshly extracted result (new signed URLs) so the NEXT download is fast too."""
    record["result"] = result
    await state_set(_extraction_key(record["id"]), record, max(1, int(record["expires_at"] - time.time())))


# ----------------------------------------------------------------------------
# Download: pick candidates -> open first that works -> stream
# ----------------------------------------------------------------------------
def _sanitize_name(name: str) -> str:
    keep = "".join(c if (c.isalnum() or c in " ._-") else " " for c in name)
    return (" ".join(keep.split()) or "media")[:120]


def _content_disposition(filename: str) -> str:
    safe = _sanitize_name(filename)
    ascii_name = safe.encode("ascii", "ignore").decode() or "media"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(safe)}"


_RANGE_RE = re.compile(r"^bytes=\d*-\d*$")


def _clean_range(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value if _RANGE_RE.match(value) and value != "bytes=-" else None


def pick_candidates(result: dict, fmt_id: str) -> Tuple[List[Tuple[str, dict, Optional[str]]], bool]:
    """Ordered (url, headers, cookies) candidates for an asset, and whether the asset exists.
    'best' falls back through the other IP-free rows (e.g. HD -> SD) before giving up."""
    out: List[Tuple[str, dict, Optional[str]]] = []

    def add(url: Optional[str], headers: Optional[dict], cookies: Optional[str]) -> None:
        if url and all(url != o[0] for o in out):
            out.append((url, dict(headers or {}), cookies))

    if not fmt_id or fmt_id == "best":
        vids = result.get("formats", {}).get("video") or []
        headers = result.get("download_headers") or (vids[0].get("headers") if vids else {})
        add(result.get("download_url"), headers, result.get("download_cookies"))
        for row in vids:
            if row.get("ip_free") is not False:
                add(row.get("url"), row.get("headers"), row.get("cookies"))
        return out, True
    for group in ("video", "audio"):
        for fmt in result.get("formats", {}).get(group, []):
            if str(fmt.get("format_id")) == fmt_id:
                add(fmt.get("url"), fmt.get("headers"), fmt.get("cookies"))
                return out, True
    for item in result.get("gallery", []):
        if str(item.get("format_id")) == fmt_id:
            add(item.get("url"), item.get("headers"), item.get("cookies"))
            return out, True
    return out, False


_BAD_CTYPES = ("text/", "application/json", "application/xml", "application/xhtml")


async def open_upstream(url: str, headers: dict, cookies: Optional[str],
                        range_header: Optional[str]) -> Tuple[Optional[httpx.Response], str]:
    """Open one upstream media URL for streaming. Follows up to 3 redirects, re-validating
    every hop (https, public host) and dropping credentials when the host changes.
    Returns (response, 'ok') or (None, reason)."""
    h = {**BASE_HEADERS, **{k: str(v) for k, v in (headers or {}).items() if "\n" not in str(v) and "\r" not in str(v)}}
    h["Accept-Encoding"] = "identity"   # raw bytes: Content-Length must match what we relay
    if cookies and "\n" not in cookies and "\r" not in cookies:
        h["Cookie"] = cookies
    if range_header:
        h["Range"] = range_header
    current = url
    for _hop in range(4):
        if not media_url_ok(current):
            return None, "blocked_url"
        try:
            # httpx: the timeout belongs on the request (AsyncClient.send() has no timeout argument).
            # `read` is per-chunk, so it bounds time-to-first-byte AND mid-stream stalls.
            req = http().build_request("GET", current, headers=h,
                                       timeout=httpx.Timeout(connect=4.0, read=12.0, write=5.0, pool=5.0))
            resp = await http().send(req, stream=True)
        except httpx.PoolTimeout:
            return None, "pool_exhausted"
        except httpx.HTTPError as exc:
            return None, type(exc).__name__
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location")
            await resp.aclose()
            if not loc:
                return None, "bad_redirect"
            nxt = urllib.parse.urljoin(current, loc)
            if urllib.parse.urlparse(nxt).netloc != urllib.parse.urlparse(current).netloc:
                h.pop("Cookie", None)
                h.pop("Authorization", None)
            current = nxt
            continue
        if resp.status_code in (200, 206):
            ctype = (resp.headers.get("content-type") or "").lower()
            if ctype.startswith(_BAD_CTYPES):   # CDN error pages served with a 200
                await resp.aclose()
                return None, "bad_content_type"
            return resp, "ok"
        status = resp.status_code
        await resp.aclose()
        return None, f"http_{status}"
    return None, "too_many_redirects"


def stream_response(resp: httpx.Response, filename: str, ext: Optional[str], kind: str) -> StreamingResponse:
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", ctype) or ctype in ("application/octet-stream", "binary/octet-stream"):
        ctype = _mime_type(ext, kind)
    headers = {"Content-Disposition": _content_disposition(filename), "Cache-Control": "no-store",
               "Accept-Ranges": "bytes", "X-Content-Type-Options": "nosniff"}
    for k in ("Content-Length", "Content-Range"):
        v = resp.headers.get(k)
        if v and re.fullmatch(r"[0-9a-zA-Z /*-]{1,64}", v):
            headers[k] = v

    async def body():
        try:
            async for chunk in resp.aiter_raw(65536):
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(body(), status_code=206 if resp.status_code == 206 else 200, media_type=ctype,
                             headers=headers, background=BackgroundTask(resp.aclose))


async def stream_asset(request: Request, page_url: str, fmt_id: str, filename: str, *, v1: bool,
                       record: Optional[dict] = None, ext: Optional[str] = None, kind: str = "video"):
    """Serve an asset.

    Round 0 uses the stored result (no extraction at all on the hot path). If no candidate
    opens, round 1 does ONE fresh extraction (new signed URLs) and tries again. v5 instead
    re-extracted on every download, burned ~20s per failure, then returned 502."""
    range_header = _clean_range(request.headers.get("range"))
    if fmt_id.startswith("mux:"):
        return await mux_download(page_url, fmt_id, filename, v1, record)
    deadline = time.monotonic() + DOWNLOAD_OPEN_BUDGET
    result = record.get("result") if record else None
    reasons: List[str] = []
    for round_ in (0, 1):
        if result is None or round_ == 1:
            remaining = deadline - time.monotonic()
            if remaining < 3:
                break
            try:
                result = await get_result(page_url, v1=v1, fresh=(round_ == 1),
                                          budget=min(remaining, _budget(page_url, v1)))
            except asyncio.TimeoutError:
                raise HTTPException(504, "Preparing the download timed out. Tap Download once more — retries usually succeed.")
            except (UserFacingError, ValueError) as e:
                raise HTTPException(400, str(e)[:300])
            if result.get("blocked"):
                raise HTTPException(409, "This media is temporarily unavailable.")
            if record is not None:
                await refresh_record(record, result)
        cands, found = pick_candidates(result, fmt_id)
        if not found:
            if round_ == 0:
                result = None  # a fresh extraction may list it
                continue
            raise HTTPException(404, "That quality is no longer listed. Extract again.")
        for url, headers, cookies in cands:
            if time.monotonic() > deadline:
                break
            resp, why = await open_upstream(url, headers, cookies, range_header)
            if resp is not None:
                return stream_response(resp, filename, ext, kind)
            reasons.append(f"r{round_}:{why}")
            log("upstream_open_failed", host=_source_host(url), why=why, round=round_, platform=result.get("platform"))
    log("download_failed", platform=(result or {}).get("platform"), reasons=reasons[-6:])
    raise HTTPException(502, "The media source didn't respond. Please try again.", headers={"Retry-After": "2"})


async def mux_download(page_url: str, format_id: str, filename: str, v1: bool, record: Optional[dict]):
    """Stream merged video+audio: ffmpeg stream-copies two tracks, no re-encode, no temp file."""
    plan = None
    result = record.get("result") if record else None
    for fresh in (False, True):
        if result is None or fresh:
            try:
                result = await get_result(page_url, v1=v1, fresh=fresh)
            except asyncio.TimeoutError:
                raise HTTPException(504, "Preparing the download timed out. Tap Download once more.")
            except (UserFacingError, ValueError) as e:
                raise HTTPException(400, str(e)[:300])
        plan = next((p for p in _mux_pairs(result) if p["format_id"] == format_id), None)
        if plan:
            break
    if not plan:
        raise HTTPException(404, "That quality is no longer listed. Extract again.")
    ff = ffmpeg_path()
    if not ff:
        raise HTTPException(501, "Merging video and audio isn't available on this server.")
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-nostdin"]
    for fmt in (plan["_video"], plan["_audio"]):
        if not media_url_ok(fmt["url"]):
            raise HTTPException(502, "Could not merge the video and audio tracks. Extract again.")
        hdrs = {k: str(v) for k, v in (fmt.get("headers") or {}).items()}
        if fmt.get("cookies"):
            hdrs["Cookie"] = fmt["cookies"]
        hdrs = {k: v for k, v in hdrs.items() if "\n" not in v and "\r" not in v and "\n" not in k and "\r" not in k}
        # Whitelist: an upstream-supplied playlist must not make ffmpeg read file:/concat:/etc.
        cmd += ["-protocol_whitelist", "https,tls,tcp,crypto", "-rw_timeout", "20000000"]
        if hdrs:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())]
        cmd += ["-i", fmt["url"]]
    cont = plan["ext"]
    cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c", "copy"]
    cmd += ["-f", "webm"] if cont == "webm" else ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4"]
    cmd += ["pipe:1"]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    async def _kill() -> None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()

    try:
        first = await asyncio.wait_for(proc.stdout.read(65536), timeout=25)
    except asyncio.TimeoutError:
        await _kill()
        raise HTTPException(504, "Merging timed out. Extract again.")
    if not first:
        err = (await proc.stderr.read() or b"").decode("utf-8", "replace").strip()[:200]
        await _kill()
        log("mux_failed", err=err)
        raise HTTPException(502, "Could not merge the video and audio tracks. Extract again.")

    async def _iter():
        try:
            yield first
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            await _kill()

    return StreamingResponse(_iter(), media_type="video/webm" if cont == "webm" else "video/mp4", headers={
        "Content-Disposition": _content_disposition(filename), "Cache-Control": "no-store",
        "Accept-Ranges": "none", "X-Content-Type-Options": "nosniff"})


# ----------------------------------------------------------------------------
# App + middleware
# ----------------------------------------------------------------------------
app = FastAPI(
    title="toolz-downloadz-api",
    description="Media extraction for TikTok and Instagram",
    version=VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url="/openapi.json" if DOCS_ENABLED else None,
)


class SecurityHeaders:
    """Pure-ASGI (no BaseHTTPMiddleware: that wrapper breaks/buffers streaming responses)."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        api = scope.get("path", "").startswith("/api/")

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("X-Frame-Options", "DENY")
                if api:
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.inner(scope, receive, send_wrapper)


app.add_middleware(SecurityHeaders)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,            # bearer tokens, not cookies
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Length", "Content-Range", "Accept-Ranges", "Retry-After"],
    max_age=86400,                      # browsers cache the preflight for a day
)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log("unhandled_error", path=request.url.path, type=type(exc).__name__, msg=str(exc)[:200])
    return JSONResponse(status_code=500, content={"detail": "The download service encountered an unexpected error. Please retry."})


@app.get("/api/health")
async def health():
    return {
        "status": "online", "service": "toolz-downloadz-api", "version": VERSION,
        "platforms": SUPPORTED, "api_versions": ["legacy", "v1"],
        "state": "upstash-redis" if redis_enabled() else "local-development",
    }


@app.get("/")
async def root():
    out = {"name": "toolz-downloadz-api", "version": VERSION, "health": "/api/health"}
    if DOCS_ENABLED:
        out["docs"] = "/docs"
    return out


_ANALYTICS = '''
<script>
  window.va = window.va || function () { (window.vaq = window.vaq || []).push(arguments); };
</script>
<script defer src="/_vercel/insights/script.js"></script>'''

if DOCS_ENABLED:
    @app.get("/docs", include_in_schema=False)
    async def custom_swagger_ui_html():
        html = get_swagger_ui_html(
            openapi_url=app.openapi_url, title=f"{app.title} - Swagger UI",
            oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
            swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
            swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
        )
        return HTMLResponse(html.body.decode("utf-8").replace("</head>", f"{_ANALYTICS}</head>"))

    @app.get("/redoc", include_in_schema=False)
    async def custom_redoc_html():
        html = get_redoc_html(
            openapi_url=app.openapi_url, title=f"{app.title} - ReDoc",
            redoc_js_url="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js",
        )
        return HTMLResponse(html.body.decode("utf-8").replace("</head>", f"{_ANALYTICS}</head>"))


@app.get("/api/platforms")
async def platforms():
    return {"platforms": [
        {"id": "tiktok", "name": "TikTok", "color": "#000000"},
        {"id": "instagram", "name": "Instagram", "color": "gradient"},
    ]}


@app.get("/api/v1/platforms")
async def v1_platforms():
    return {"platforms": [
        {"id": "tiktok", "name": "TikTok", "media": ["video", "audio", "gallery"]},
        {"id": "instagram", "name": "Instagram", "media": ["video", "audio", "gallery"]},
        {"id": "youtube", "name": "YouTube", "media": ["video", "audio"]},
    ]}


# ----------------------------------------------------------------------------
# Legacy routes (API-key protected; fail closed in production without a key)
# ----------------------------------------------------------------------------
async def do_extract(request: Request, url: str, audio_only: bool, custom_format: Optional[str]):
    if not await rate_ok("legacy:" + client_ip(request)):
        raise HTTPException(status_code=429, detail=f"Rate limit exceeded ({RATE_LIMIT}/min). Slow down.",
                            headers={"Retry-After": "30"})
    check_auth(request)
    url = clean_url(url)
    if not detect_platform(url):
        raise HTTPException(status_code=400, detail="Unsupported URL. Only TikTok and Instagram are supported.")
    cache_key = "downloadz:v2:result:" + _ckey(_key_url(url), f"False|{audio_only}|{custom_format}")
    cached = await state_get(cache_key)
    if cached:
        return {**cached, "_cached": True}
    try:
        return await get_result(url, audio_only=audio_only, custom_format=custom_format, v1=False)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail=f"Extraction timed out after {EXTRACT_TIMEOUT}s. Retry — repeated attempts get faster (cache).")
    except (UserFacingError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)[:300])


@app.get("/api/extract")
async def extract_get(
    request: Request,
    url: str = Query(..., min_length=8, max_length=2048),
    audio_only: bool = Query(False),
    format: Optional[str] = Query(None, max_length=64),
):
    return await do_extract(request, url, audio_only, format)


@app.post("/api/extract")
async def extract_post(request: Request):
    body = await read_json(request)
    url = body.get("url")
    if not url or not isinstance(url, str):
        raise HTTPException(status_code=400, detail="Missing 'url' in body")
    fmt = body.get("format")
    return await do_extract(request, url, bool(body.get("audio_only")), fmt if isinstance(fmt, str) else None)


@app.get("/api/download")
async def download(
    request: Request,
    u: str = Query(..., max_length=2048, description="Original page URL"),
    f: str = Query("best", max_length=96, description="yt-dlp format_id, or 'best'"),
    n: str = Query("", max_length=200, description="Filename"),
):
    """Legacy downloader. New clients use authenticated v1 asset paths."""
    if not await rate_ok("legacy-dl:" + client_ip(request), RATE_LIMIT * 2):
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Slow down.", headers={"Retry-After": "30"})
    check_auth(request, allow_query_key=True)  # ?key= only here, for browser navigation
    page_url = clean_url(u)
    if not detect_platform(page_url):
        raise HTTPException(status_code=400, detail="Unsupported URL")
    return await stream_asset(request, page_url, f, n or "media", v1=False)


# ----------------------------------------------------------------------------
# v1 public client contract: never returns upstream media URLs, cookies, headers
# ----------------------------------------------------------------------------
@app.post("/api/v1/client-sessions")
async def create_client_session(request: Request):
    ip = client_ip(request)
    if not await rate_ok("session:" + ip, SESSION_RATE):
        raise HTTPException(status_code=429, detail="Too many session requests. Try again shortly.",
                            headers={"Retry-After": "30"})
    body = await read_json(request)
    return await issue_guest_session(str(body.get("installation_id") or ""), ip)


@app.post("/api/v1/extractions")
async def create_v1_extraction(request: Request):
    token, session = await require_guest_session(request)
    ip = client_ip(request)
    if not (await rate_ok("extract:" + _h(token)[:16]) and await rate_ok("extract-ip:" + ip, RATE_LIMIT * 2)):
        raise HTTPException(status_code=429, detail="Too many extraction requests. Wait a minute and retry.",
                            headers={"Retry-After": "30"})
    body = await read_json(request)
    url = clean_url(str(body.get("url") or ""))
    if not detect_v1_platform(url):
        raise HTTPException(status_code=400, detail="Unsupported URL. Use a public TikTok, Instagram, or YouTube link.")
    audio_only = bool(body.get("audio_only"))
    try:
        result = await get_result(url, audio_only=audio_only, v1=True)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Extraction timed out. Retry in a moment.")
    except (UserFacingError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)[:600])
    except Exception as exc:  # unknown failure: log it, don't leak it, and let the client retry
        log("extract_error", type=type(exc).__name__, msg=str(exc)[:200])
        raise HTTPException(status_code=503, detail=GENERIC_FAIL, headers={"Retry-After": "3"})
    if result.get("blocked"):
        raise HTTPException(status_code=422, detail=result.get("blocked_message") or "This media is unavailable.")
    record = build_v1_record(url, result, str(session["installation_hash"]))
    await state_set(_extraction_key(record["id"]), record, EXTRACTION_TTL)
    return public_v1_extraction(record)


@app.get("/api/v1/extractions/{extraction_id}")
async def get_v1_extraction(extraction_id: str, request: Request):
    _token, session = await require_guest_session(request)
    record = await load_record(extraction_id, session, "Extraction expired or was not found.")
    return public_v1_extraction(record)


@app.get("/api/v1/extractions/{extraction_id}/assets/{asset_id}/download")
async def download_v1_asset(extraction_id: str, asset_id: str, request: Request):
    token, session = await require_guest_session(request)
    if not await rate_ok("dl:" + _h(token)[:16], DOWNLOAD_RATE, shared=False):
        raise HTTPException(status_code=429, detail="Too many downloads. Wait a minute and retry.",
                            headers={"Retry-After": "30"})
    record = await load_record(extraction_id, session, "Download expired. Extract the link again.")
    asset = (record.get("assets") or {}).get(asset_id) if ASSET_ID_RE.match(asset_id or "") else None
    if not asset:
        raise HTTPException(status_code=404, detail="The selected format is no longer available.")
    result = record.get("result") or {}
    ext = re.sub(r"[^a-z0-9]", "", str(asset.get("ext") or "mp4").lower())[:5] or "mp4"
    title = _sanitize_name(result.get("title") or "media")
    suffix = f"-{asset_id}" if asset.get("kind") == "image" else ""
    filename = f"{title}{suffix}.{ext}"
    if result.get("source") == "ytapi":
        # googlevideo bytes may only be fetched by the process that extracted them (IP-bound):
        # hand the client a signed ticket to that service.
        return RedirectResponse(
            _ytapi_download_url(str(result.get("video_id") or ""), str(asset["format"]), filename, asset.get("height")),
            status_code=307, headers={"Cache-Control": "no-store"})
    return await stream_asset(request, record["source_url"], str(asset["format"]), filename, v1=True,
                              record=record, ext=ext, kind=str(asset.get("kind") or "video"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.index:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=_bool_env("RELOAD", True))
