"""
toolz-downloadz-api v3.0 — single-file, self-contained FastAPI app for Vercel.
Platforms: YouTube, TikTok, Instagram Reels ONLY.

Why single file: Vercel's Python builder is picky about packages inside api/.
A flat, dependency-free-import file eliminates the NOT_FOUND class of bugs.

Run locally:  uvicorn api.index:app --reload   (or: python api/index.py)
"""
import os
import time
import json
import asyncio
import hashlib
import urllib.request
import urllib.parse
from collections import defaultdict, deque
from typing import Optional, Dict, Any, List, Tuple

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from dotenv import load_dotenv

load_dotenv()
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# ----------------------------------------------------------------------------
# Config (env)
# ----------------------------------------------------------------------------
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "").strip()
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES", "").strip()
INSTAGRAM_COOKIES = os.getenv("INSTAGRAM_COOKIES", "").strip()
EXTRACT_TIMEOUT = int(os.getenv("EXTRACT_TIMEOUT", "8"))     # seconds; hobby-safe default
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "30"))              # per minute per key
VERSION = "3.0.0"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
BASE_HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

SUPPORTED = ["youtube", "tiktok", "instagram"]

YT_CLIENT_STRATEGIES = [
    ["android", "web_safari", "mweb"],
    ["ios", "tv"],
]

# ----------------------------------------------------------------------------
# Tiny in-memory cache + rate limiter (per-lambda; zero infra)
# ----------------------------------------------------------------------------
_cache: Dict[str, Tuple[float, dict]] = {}


def _ckey(url: str, opts: str) -> str:
    return hashlib.sha256(f"{url}|{opts}".encode()).hexdigest()


def cache_get(key: str) -> Optional[dict]:
    hit = _cache.get(key)
    if not hit:
        return None
    exp, val = hit
    if time.time() > exp:
        _cache.pop(key, None)
        return None
    return val


def cache_set(key: str, val: dict, ttl: int = CACHE_TTL) -> None:
    if len(_cache) > 500:
        for k, _ in sorted(_cache.items(), key=lambda x: x[1][0])[:100]:
            _cache.pop(k, None)
    _cache[key] = (time.time() + ttl, val)


_hits: Dict[str, deque] = defaultdict(lambda: deque(maxlen=60))


def rate_ok(ident: str) -> bool:
    now = time.time()
    q = _hits[ident]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return False
    q.append(now)
    return True


# ----------------------------------------------------------------------------
# Cookies (env content -> /tmp file for yt-dlp)
# ----------------------------------------------------------------------------
def _cookies_file(content: str, name: str) -> Optional[str]:
    if not content:
        return None
    try:
        p = os.path.join("/tmp", name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content if content.endswith("\n") else content + "\n")
        return p
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Platform detection — strict allowlist
# ----------------------------------------------------------------------------
def detect_platform(url: str) -> Optional[str]:
    u = url.lower()
    if any(d in u for d in ("youtube.com", "youtu.be")):
        return "youtube"
    if "tiktok.com" in u:
        return "tiktok"
    if "instagram.com" in u and ("/reel" in u or "/reels" in u or "/p/" in u):
        return "instagram"
    return None


def tiktok_canonical(url: str) -> str:
    return url.split("?")[0].split("#")[0]


# ----------------------------------------------------------------------------
# oEmbed fallbacks (public endpoints, never blocked)
# ----------------------------------------------------------------------------
def _oembed(endpoint_url: str) -> Optional[dict]:
    try:
        req = urllib.request.Request(endpoint_url, headers=BASE_HEADERS)
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8"))
        return {
            "title": data.get("title"),
            "uploader": data.get("author_name"),
            "uploader_url": data.get("author_url"),
            "thumbnail": data.get("thumbnail_url"),
        }
    except Exception:
        return None


def youtube_oembed(url: str) -> Optional[dict]:
    return _oembed("https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe=""))


def tiktok_oembed(url: str) -> Optional[dict]:
    return _oembed("https://www.tiktok.com/oembed?url=" + urllib.parse.quote(tiktok_canonical(url), safe=""))


# ----------------------------------------------------------------------------
# yt-dlp options per platform
# ----------------------------------------------------------------------------
def ydl_opts(platform: str, audio_only: bool = False, custom_format: Optional[str] = None,
             yt_clients: Optional[List[str]] = None) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "socket_timeout": 10,
        "geo_bypass": True,
        "http_headers": BASE_HEADERS,
        "extractor_args": {},
    }
    if platform == "youtube":
        cf = _cookies_file(YOUTUBE_COOKIES, "yt_cookies.txt")
        if cf:
            opts["cookiefile"] = cf
        opts["format"] = custom_format or ("bestaudio/best" if audio_only else "best[ext=mp4]/best")
        opts["youtube_include_dash_manifest"] = False
        opts["youtube_include_hls_manifest"] = False
        opts["extractor_args"]["youtube"] = {"player_client": yt_clients or YT_CLIENT_STRATEGIES[0]}
    elif platform == "tiktok":
        # NOTE: no api_hostname override — defaults are what currently work
        opts["format"] = custom_format or ("bestaudio/best" if audio_only else "best")
    elif platform == "instagram":
        cf = _cookies_file(INSTAGRAM_COOKIES, "ig_cookies.txt")
        if cf:
            opts["cookiefile"] = cf
        opts["format"] = custom_format or ("bestaudio/best" if audio_only else "best")
    return opts


def run_ydl(opts: Dict[str, Any], url: str) -> dict:
    import yt_dlp
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


# ----------------------------------------------------------------------------
# pytubefix fallback engine for YouTube
# ----------------------------------------------------------------------------
def youtube_pytubefix(url: str) -> Optional[dict]:
    try:
        from pytubefix import YouTube
    except Exception:
        return None
    try:
        yt = YouTube(url)
        video, audio = [], []
        for s in yt.streams:
            if not s.url:
                continue
            fmt = {
                "format_id": s.itag,
                "ext": (s.mime_type.split("/")[-1] if s.mime_type else "mp4"),
                "resolution": s.resolution or s.abr or "audio",
                "url": s.url,
                "filesize": s.filesize,
                "vcodec": "none" if not s.resolution else "avc1",
                "acodec": "mp4a" if s.abr else None,
                "headers": {"User-Agent": "com.google.android.youtube/19.09.37"},
            }
            (audio if (s.abr or not s.resolution) else video).append(fmt)
        dl = next((s.url for s in yt.streams.filter(progressive=True, file_extension="mp4")), None)
        return {
            "platform": "youtube", "title": yt.title, "thumbnail": yt.thumbnail_url,
            "duration": getattr(yt, "length", None), "uploader": yt.author,
            "uploader_url": None,
            "stats": {}, "upload_date": None, "description": None,
            "download_url": dl, "download_headers": {"User-Agent": "com.google.android.youtube/19.09.37"},
            "ext": "mp4", "blocked": False,
            "formats": {"video": video, "audio": audio}, "original_url": url,
        }
    except Exception:
        return None


# ----------------------------------------------------------------------------
# Response shaping
# ----------------------------------------------------------------------------
def normalize(info: dict) -> Tuple[List[dict], List[dict]]:
    video, audio = [], []
    for f in info.get("formats") or []:
        if not f.get("url"):
            continue
        fmt = {
            "format_id": f.get("format_id"), "ext": f.get("ext"),
            "resolution": f.get("resolution") or f.get("format_note") or "unknown",
            "url": f["url"],
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "vcodec": f.get("vcodec"), "acodec": f.get("acodec"),
            "height": f.get("height"), "tbr": f.get("tbr"), "abr": f.get("abr"),
            "headers": dict(f.get("http_headers") or {}),
            "cookies": f.get("cookies"),  # TikTok needs ttwid etc. per-format
        }
        (audio if f.get("vcodec") == "none" else video).append(fmt)

    def vh(x):
        return (x.get("height") or 0, x.get("tbr") or 0, x.get("filesize") or 0)

    def ah(x):
        return (x.get("abr") or x.get("tbr") or 0, x.get("filesize") or 0)

    video.sort(key=vh, reverse=True)
    audio.sort(key=ah, reverse=True)
    return video, audio


def shape(platform: str, info: dict, original_url: str) -> dict:
    video, audio = normalize(info)
    dl = info.get("url")
    hdrs = dict(info.get("http_headers") or {})
    if not dl:
        merged = [f for f in video if f["vcodec"] not in (None, "none") and f["acodec"] not in (None, "none")]
        pick = merged or video or audio
        if pick:
            dl, hdrs = pick[0]["url"], dict(pick[0]["headers"] or {})
    stats = {k: info.get(k) for k in
             ("view_count", "like_count", "comment_count")}
    # Pair the chosen download_url with ITS OWN cookies (TikTok tt_chain_token
    # must match the ?tk= param inside that exact URL or CDN returns 403)
    dl_cookies = None
    if dl:
        for f_ in info.get("formats") or []:
            if f_.get("url") == dl and f_.get("cookies"):
                dl_cookies = f_["cookies"]
                break
    return {
        "platform": platform,
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "uploader_url": info.get("uploader_url"),
        "stats": stats,
        "upload_date": info.get("upload_date"),
        "description": (info.get("description") or "")[:400] or None,
        "download_url": dl,
        "download_headers": hdrs,
        "download_cookies": dl_cookies,
        "ext": info.get("ext"),
        "blocked": False,
        "formats": {"video": video[:20], "audio": audio[:10]},
        "original_url": original_url,
    }


def blocked_shape(platform: str, meta: dict, original_url: str, msg: str) -> dict:
    return {
        "platform": platform, "title": meta.get("title"), "thumbnail": meta.get("thumbnail"),
        "duration": None, "uploader": meta.get("uploader"), "uploader_url": meta.get("uploader_url"),
        "stats": {}, "upload_date": None, "description": None,
        "download_url": None, "download_headers": {},
        "ext": None, "blocked": True, "blocked_message": msg,
        "formats": {"video": [], "audio": []}, "original_url": original_url,
    }


BLOCK_MSGS = {
    "youtube": ("YouTube is blocking this server IP right now (common on Vercel). "
                "Metadata below is from YouTube's public oEmbed. Fix: set YOUTUBE_COOKIES env "
                "(Netscape cookies.txt from your browser) and redeploy, or retry later."),
    "tiktok": ("TikTok extraction failed on this server IP. "
               "Metadata below is from TikTok's public oEmbed. Try again in a minute."),
}


# ----------------------------------------------------------------------------
# Core extraction orchestration
# ----------------------------------------------------------------------------
def extract_sync(url: str, audio_only: bool = False, custom_format: Optional[str] = None) -> dict:
    platform = detect_platform(url)
    if not platform:
        raise ValueError("Unsupported URL. Only YouTube, TikTok and Instagram Reels are supported.")

    if platform == "youtube":
        last = None
        for clients in YT_CLIENT_STRATEGIES:
            try:
                return shape(platform, run_ydl(ydl_opts("youtube", audio_only, custom_format, clients), url), url)
            except Exception as e:
                last = str(e)
                if "Sign in to confirm" in last or "not a bot" in last:
                    break  # IP-flagged; rotating clients won't help
        alt = youtube_pytubefix(url)
        if alt:
            return alt
        meta = youtube_oembed(url)
        if meta:
            return blocked_shape("youtube", meta, url, BLOCK_MSGS["youtube"])
        raise RuntimeError(f"YouTube extraction failed: {(last or 'unknown')[:300]}")

    if platform == "tiktok":
        last = None
        candidates = list(dict.fromkeys([url, tiktok_canonical(url)]))  # dedupe, keep order
        for attempt in range(2):  # TikTok anti-bot is intermittent; retry once
            for candidate in candidates:
                try:
                    return shape(platform, run_ydl(ydl_opts("tiktok", audio_only, custom_format), candidate), url)
                except Exception as e:
                    last = str(e)
            time.sleep(0.8)
        meta = tiktok_oembed(url)
        if meta:
            return blocked_shape("tiktok", meta, url, BLOCK_MSGS["tiktok"])
        raise RuntimeError(f"TikTok extraction failed: {(last or 'unknown')[:300]}")

    # instagram reels
    try:
        return shape(platform, run_ydl(ydl_opts("instagram", audio_only, custom_format), url), url)
    except Exception as e:
        msg = str(e).replace("ERROR: ", "").split(" Check if this post")[0].strip()
        hint = ("Instagram requires login for most content on server IPs. "
                "Set INSTAGRAM_COOKIES env (Netscape cookies.txt from a logged-in browser) and redeploy.")
        raise RuntimeError(f"{msg[:250]} ({hint})")


# ----------------------------------------------------------------------------
# Auth / validation helpers
# ----------------------------------------------------------------------------
def check_auth(request: Request) -> None:
    if not API_SECRET_KEY:
        return  # dev mode without key
    provided = (request.headers.get("x-api-key") or "").strip()
    if not provided:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
    if not provided:
        provided = (request.query_params.get("key") or "").strip()
    if provided != API_SECRET_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing API key")


def clean_url(url: str) -> str:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    if len(url) > 2048:
        raise HTTPException(status_code=400, detail="URL too long")
    host = urllib.parse.urlparse(url).hostname or ""
    if host.split(":")[0] in ("localhost", "127.0.0.1", "0.0.0.0", "::1") or \
       host.endswith((".local", ".internal")) or host.startswith(("10.", "192.168.", "169.254.")) or \
       host.startswith("172.16.") or host.startswith("172.17.") or host.startswith("172.18.") or \
       host.startswith("172.19.") or host.startswith("172.2") or host.startswith("172.30.") or host.startswith("172.31."):
        raise HTTPException(status_code=400, detail="Private/internal hosts are not allowed")
    return url


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------
app = FastAPI(
    title="toolz-downloadz-api",
    description="Media extraction for YouTube, TikTok and Instagram Reels",
    version=VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={"detail": f"Internal error: {str(exc)[:200]}"})


@app.get("/api/health")
async def health():
    return {
        "status": "online",
        "service": "toolz-downloadz-api",
        "version": VERSION,
        "platforms": SUPPORTED,
        "auth": bool(API_SECRET_KEY),
        "cookies": {"youtube": bool(YOUTUBE_COOKIES), "instagram": bool(INSTAGRAM_COOKIES)},
    }


@app.get("/")
async def root():
    return {"name": "toolz-downloadz-api", "version": VERSION, "docs": "/docs", "health": "/api/health"}


@app.get("/api/platforms")
async def platforms():
    return {"platforms": [
        {"id": "youtube", "name": "YouTube", "color": "#FF0000"},
        {"id": "tiktok", "name": "TikTok", "color": "#000000"},
        {"id": "instagram", "name": "Instagram Reels", "color": "gradient"},
    ]}


async def do_extract(request: Request, url: str, audio_only: bool, custom_format: Optional[str]):
    ident = request.client.host if request.client else "anon"
    if not rate_ok(ident):
        raise HTTPException(status_code=429, detail=f"Rate limit exceeded ({RATE_LIMIT}/min). Slow down.")
    check_auth(request)
    url = clean_url(url)
    platform = detect_platform(url)
    if not platform:
        raise HTTPException(status_code=400, detail="Unsupported URL. Only YouTube, TikTok and Instagram Reels are supported.")

    key = _ckey(url, f"{audio_only}|{custom_format}")
    cached = cache_get(key)
    if cached:
        out = dict(cached)
        out["_cached"] = True
        return out

    try:
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: extract_sync(url, audio_only, custom_format)),
            timeout=max(EXTRACT_TIMEOUT, 5),
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"Extraction timed out after {EXTRACT_TIMEOUT}s. Retry — repeated attempts get faster (cache).",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not result.get("blocked"):
        cache_set(key, result)
    return result


@app.get("/api/extract")
async def extract_get(
    request: Request,
    url: str = Query(..., min_length=8, max_length=2048),
    audio_only: bool = Query(False),
    format: Optional[str] = Query(None),
):
    return await do_extract(request, url, audio_only, format)


@app.post("/api/extract")
async def extract_post(request: Request, body: Dict[str, Any] = None):
    try:
        body = body or await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    url = body.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="Missing 'url' in body")
    return await do_extract(request, url, bool(body.get("audio_only")), body.get("format"))


# ----------------------------------------------------------------------------
# One-step download: resolve formats + stream media FROM THE SAME LAMBDA.
# Critical because YouTube/TikTok sign media URLs to the requesting IP — a
# separate proxy server gets 403. Same-instance fetch keeps the signature valid.
# ----------------------------------------------------------------------------
def _sanitize_name(name: str) -> str:
    keep = "".join(c if (c.isalnum() or c in " ._-") else " " for c in name)
    return (" ".join(keep.split()) or "media")[:120]


@app.get("/api/download")
async def download(
    request: Request,
    u: str = Query(..., description="Original page URL"),
    f: str = Query("best", description="yt-dlp format_id, or 'best'"),
    n: str = Query("", description="Filename"),
):
    ident = request.client.host if request.client else "anon"
    check_auth(request)  # supports ?key= for browser navigation
    page_url = clean_url(u)
    platform = detect_platform(page_url)
    if not platform:
        raise HTTPException(status_code=400, detail="Unsupported URL")

    # Resolve media URL. Attempt 0 uses cache (instant); on expired/403 links we
    # re-extract FRESH signatures once and retry before giving up.
    range_header = request.headers.get("range")
    last_err = None

    for attempt in range(2):
        key = _ckey(page_url, "False|None")
        result = cache_get(key) if attempt == 0 else None
        if not result or result.get("blocked"):
            loop = asyncio.get_running_loop()
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: extract_sync(page_url)), timeout=max(EXTRACT_TIMEOUT, 5)
                )
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"Could not resolve media: {str(e)[:200]}")
            if result.get("blocked"):
                raise HTTPException(status_code=409, detail=result.get("blocked_message", "Extraction blocked"))
            cache_set(key, result)

        # Pick format
        headers = dict(result.get("download_headers") or {})
        fmt_cookies = None
        if f == "best" or not f:
            media = result.get("download_url")
            fmt_cookies = result.get("download_cookies")  # its OWN cookies (token pairing)
        else:
            media = None
            for group in ("video", "audio"):
                for fmt in result.get("formats", {}).get(group, []):
                    if str(fmt.get("format_id")) == f:
                        media, headers = fmt["url"], dict(fmt.get("headers") or {})
                        fmt_cookies = fmt.get("cookies")
                        break
                if media:
                    break
            if not media and result.get("download_url"):
                media = result.get("download_url")
                fmt_cookies = result.get("download_cookies")
        if not media:
            raise HTTPException(status_code=404, detail="Format not found — re-extract the link first")

        if fmt_cookies:
            headers["Cookie"] = fmt_cookies
        if range_header:
            headers["Range"] = range_header

        def _open(extra=None):
            h = dict(headers)
            if extra:
                h.update(extra)
            req = urllib.request.Request(media, headers=h)
            return urllib.request.urlopen(req, timeout=20)

        try:
            try:
                resp = _open()
            except urllib.error.HTTPError as e:
                if e.code in (403, 410) and attempt == 0:
                    last_err = f"{e.code}"
                    continue  # signature likely expired → fresh extract + retry
                resp = _open({"Range": "bytes=0-"})  # some CDNs require explicit Range
        except urllib.error.HTTPError as e:
            if attempt == 0:
                last_err = str(e.code)
                continue
            raise HTTPException(status_code=502,
                                detail=f"Media source refused ({e.code}). Link expired — extract again.")
        except Exception as e:
            if attempt == 0:
                last_err = str(e)
                continue
            raise HTTPException(status_code=502, detail=f"Media fetch failed: {str(e)[:200]}")

        def _iter(r):
            try:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                r.close()

        out_headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(_sanitize_name(n))}",
            "Cache-Control": "no-store",
            "Accept-Ranges": "bytes",
        }
        for k in ("Content-Length", "Content-Range", "Content-Type"):
            v = resp.headers.get(k)
            if v:
                out_headers[k] = v
        status = 206 if resp.status == 206 else 200
        return StreamingResponse(_iter(resp), status_code=status,
                                 media_type=resp.headers.get("Content-Type", "application/octet-stream"),
                                 headers=out_headers)

    raise HTTPException(status_code=502, detail=f"Media source refused ({last_err}). Extract again.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.index:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
