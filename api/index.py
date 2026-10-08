"""
toolz-downloadz-api v4.0 — single-file, self-contained FastAPI app for Vercel.
Platforms: TikTok + Instagram ONLY.

Why single file: Vercel's Python builder is picky about packages inside api/.
A flat, dependency-free-import file eliminates the NOT_FOUND class of bugs.

Run locally:  uvicorn api.index:app --reload   (or: python api/index.py)
"""
import os
import re
import time
import json
import base64
import hmac
import shutil
import asyncio
import hashlib
import ipaddress
import secrets
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from collections import defaultdict, deque
from typing import Optional, Dict, Any, List, Tuple

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from dotenv import load_dotenv

load_dotenv()
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# ----------------------------------------------------------------------------
# Config (env)
# ----------------------------------------------------------------------------
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "").strip()
INSTAGRAM_COOKIES = os.getenv("INSTAGRAM_COOKIES", "").strip()
# YouTube works like every other working downloader (incl. cobalt's
# reference setup): yt-dlp + logged-in cookies. The cookies carry the trust
# that datacenter IPs lack. Use a THROWAWAY Google account — never your main
# one. Rotate when extractions start failing with bot-wall errors.
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES", "").strip()
# YouTube extraction microservice (toolz-ytapi: youtubei.js + same cookies).
# Python yt-dlp on serverless cannot decipher ciphered streams and may lack
# PO tokens; youtubei.js deciphers natively in JS. Used as fallback when
# yt-dlp finds zero usable formats. Unset = yt-dlp only (+ dead-link check).
YT_EXTRACT_URL = (os.getenv("YT_EXTRACT_URL") or "").strip().rstrip("/")
YTAPI_SECRET = (os.getenv("YTAPI_SECRET") or "").strip()



def _int_env(name: str, default: int) -> int:
    """Env int that never crashes the lambda on a malformed value."""
    try:
        return int((os.getenv(name) or "").strip() or default)
    except (ValueError, TypeError):
        return default


EXTRACT_TIMEOUT = _int_env("EXTRACT_TIMEOUT", 26)    # seconds; fits maxDuration=60
CACHE_TTL = _int_env("CACHE_TTL", 3600)
RATE_LIMIT = _int_env("RATE_LIMIT", 30)              # per minute per IP
GUEST_SESSION_TTL = _int_env("GUEST_SESSION_TTL", 60 * 60 * 24 * 14)
EXTRACTION_TTL = _int_env("EXTRACTION_TTL", 60 * 10)
VERSION = "5.0.0"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
BASE_HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

SUPPORTED = ["tiktok", "instagram"]
V1_SUPPORTED = ["tiktok", "instagram", "youtube"]

# ----------------------------------------------------------------------------
# Tiny in-memory cache + rate limiter (per-lambda; zero infra)
# ----------------------------------------------------------------------------
_cache: Dict[str, Tuple[float, dict]] = {}

# The v1 contract needs state that can survive a cold serverless invocation.
# Upstash Redis REST is the production store. The legacy KV names remain only
# as a rollout fallback for an existing Vercel KV configuration.
REDIS_REST_URL = (os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("KV_REST_API_URL") or "").rstrip("/")
REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("KV_REST_API_TOKEN", "")
_state: Dict[str, Tuple[float, dict]] = {}


def state_get(key: str) -> Optional[dict]:
    """Read JSON state from managed KV, with a tiny local-dev fallback."""
    if REDIS_REST_URL and REDIS_REST_TOKEN:
        try:
            req = urllib.request.Request(
                REDIS_REST_URL,
                data=json.dumps(["GET", key]).encode(),
                headers={"Authorization": f"Bearer {REDIS_REST_TOKEN}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as res:
                value = json.loads(res.read().decode()).get("result")
            return json.loads(value) if value else None
        except Exception:
            # Availability must not turn a valid request into a 500. Monitoring
            # still receives the exception through the route-level reporter.
            return None
    hit = _state.get(key)
    if not hit:
        return None
    expires_at, value = hit
    if expires_at <= time.time():
        _state.pop(key, None)
        return None
    return value


def state_set(key: str, value: dict, ttl: int) -> None:
    if REDIS_REST_URL and REDIS_REST_TOKEN:
        try:
            req = urllib.request.Request(
                REDIS_REST_URL,
                data=json.dumps(["SET", key, json.dumps(value, separators=(",", ":")), "EX", max(1, ttl)]).encode(),
                headers={"Authorization": f"Bearer {REDIS_REST_TOKEN}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3):
                pass
            return
        except Exception:
            return
    if len(_state) > 500:
        for stale in sorted(_state, key=lambda k: _state[k][0])[:100]:
            _state.pop(stale, None)
    _state[key] = (time.time() + max(1, ttl), value)


def _ckey(url: str, opts: str) -> str:
    return hashlib.sha256(f"{url}|{opts}".encode()).hexdigest()


def _key_url(url: str) -> str:
    """Cache identity: strip tracking params/fragments so one post hits one entry."""
    return url.split("?")[0].split("#")[0]


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
_cookie_hash: Dict[str, str] = {}


def _cookies_file(content: str, name: str) -> Optional[str]:
    if not content:
        return None
    p = os.path.join("/tmp", name)
    digest = hashlib.sha256(content.encode()).hexdigest()
    if _cookie_hash.get(name) == digest and os.path.isfile(p):
        return p  # warm lambda, content unchanged — skip the rewrite
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(content if content.endswith("\n") else content + "\n")
        _cookie_hash[name] = digest
        return p
    except Exception:
        return None


class UnsupportedMedia(RuntimeError):
    """Valid URL, but the content isn't downloadable (e.g. a photo slideshow)."""


# ----------------------------------------------------------------------------
# Platform detection — strict registered-domain allowlist
# ----------------------------------------------------------------------------
def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _source_host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")


def detect_platform(url: str) -> Optional[str]:
    host = _source_host(url)
    if _host_is(host, "tiktok.com"):
        return "tiktok"
    path = urllib.parse.urlparse(url).path.lower()
    if _host_is(host, "instagram.com") and (path.startswith("/reel") or path.startswith("/reels") or path.startswith("/p/")):
        return "instagram"
    return None


def detect_v1_platform(url: str) -> Optional[str]:
    """v1 adds YouTube without broad substring matching or lookalike hosts."""
    legacy = detect_platform(url)
    if legacy:
        return legacy
    host = _source_host(url)
    if (_host_is(host, "youtube.com") or _host_is(host, "youtube-nocookie.com")
            or host == "youtu.be"):
        return "youtube"
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


def tiktok_oembed(url: str) -> Optional[dict]:
    return _oembed("https://www.tiktok.com/oembed?url=" + urllib.parse.quote(tiktok_canonical(url), safe=""))


def _youtube_id(url: str) -> Optional[str]:
    """11-char video id from watch?v=, youtu.be/, shorts/, embed/, live/."""
    try:
        u = urllib.parse.urlparse(url)
    except Exception:
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


def youtube_oembed(url: str) -> Optional[dict]:
    """Public metadata probe. None = video doesn't exist / is private."""
    vid = _youtube_id(url)
    if not vid:
        return None
    return _oembed("https://www.youtube.com/oembed?url=" +
                   urllib.parse.quote(f"https://www.youtube.com/watch?v={vid}", safe="") +
                   "&format=json")


def _tikwm_fetch(url: str) -> Optional[dict]:
    """Raw tikwm payload (the `data` dict) or None on transport/API failure."""
    try:
        api = "https://www.tikwm.com/api/?hd=1&url=" + urllib.parse.quote(tiktok_canonical(url), safe="")
        req = urllib.request.Request(api, headers=BASE_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data.get("code") != 0 or not data.get("data"):
            return None
        return data["data"]
    except Exception:
        return None


def _num(value) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def _tikwm_shape(d: dict, url: str, audio_only: bool = False) -> Optional[dict]:
    """
    Build our response from a parsed tikwm payload. Pure (no I/O), so unit-testable.

    Honesty rules: only label what we observe. tikwm reports byte counts
    (size/hd_size/wm_size) and duration, but no dimensions or codecs — so the
    resolution tier is approximate and carries a ~ prefix, and codec fields
    stay null. has_audio is explicit because TikTok delivers muxed A+V files.
    """
    media = d.get("hdplay") or d.get("play") or d.get("wmplay")
    if not media:
        return None
    if d.get("hdplay"):
        size, hd = _num(d.get("hd_size")), True
    elif d.get("play"):
        size, hd = _num(d.get("size")), False
    else:
        size, hd = _num(d.get("wm_size")), False

    video = [{"format_id": "tikwm_hd" if hd else "tikwm_sd",
              "ext": "mp4",
              "resolution": ("~1080p HD, no watermark" if hd else "~SD, no watermark"),
              "url": media, "filesize": size, "vcodec": None, "acodec": None,
              "has_audio": True, "ladder": "fast", "ip_free": True,
              "height": None, "tbr": None, "abr": None,
              "headers": dict(BASE_HEADERS), "cookies": None}]
    if hd and d.get("play") and d.get("play") != media:
        # A smaller no-watermark file is a real choice (data saver / quick share).
        video.append({"format_id": "tikwm_sd", "ext": "mp4", "resolution": "~SD, no watermark",
                      "url": d["play"], "filesize": _num(d.get("size")), "vcodec": None, "acodec": None,
                      "has_audio": True, "ladder": "fast", "ip_free": True,
                      "height": None, "tbr": None, "abr": None,
                      "headers": dict(BASE_HEADERS), "cookies": None})
    audio = []
    if d.get("music"):
        audio.append({"format_id": "tikwm_music", "ext": "mp3", "resolution": "audio",
                      "url": d["music"], "filesize": None, "vcodec": "none", "acodec": None,
                      "has_audio": False, "ladder": "fast", "ip_free": True,
                      "height": None, "tbr": None, "abr": None,
                      "headers": dict(BASE_HEADERS), "cookies": None})
    author = d.get("author") or {}

    upload_date = None
    ct = _num(d.get("create_time"))
    if ct:
        upload_date = time.strftime("%Y%m%d", time.gmtime(ct))

    if audio_only and audio:
        track = audio[0]
        return {
            "platform": "tiktok",
            "title": d.get("title"),
            "thumbnail": d.get("cover") or d.get("origin_cover"),
            "duration": _num(d.get("duration")),
            "uploader": author.get("nickname"),
            "uploader_url": f"https://www.tiktok.com/@{author.get('unique_id')}" if author.get("unique_id") else None,
            "stats": {"view_count": _num(d.get("play_count")), "like_count": _num(d.get("digg_count")),
                      "comment_count": _num(d.get("comment_count"))},
            "upload_date": upload_date, "description": None,
            "download_url": track["url"], "download_headers": dict(BASE_HEADERS),
            "ext": "mp3", "blocked": False, "source": "tikwm", "ladder": "fast",
            "formats": {"video": [], "audio": audio},
            "original_url": url,
        }
    return {
        "platform": "tiktok",
        "title": d.get("title"),
        "thumbnail": d.get("cover") or d.get("origin_cover"),
        "duration": _num(d.get("duration")),
        "uploader": author.get("nickname"),
        "uploader_url": f"https://www.tiktok.com/@{author.get('unique_id')}" if author.get("unique_id") else None,
        "stats": {"view_count": _num(d.get("play_count")), "like_count": _num(d.get("digg_count")),
                  "comment_count": _num(d.get("comment_count"))},
        "upload_date": upload_date, "description": None,
        "download_url": media, "download_headers": dict(BASE_HEADERS),
        "ext": "mp4", "blocked": False, "source": "tikwm", "ladder": "fast",
        "formats": {"video": video, "audio": audio},
        "original_url": url,
    }


def tiktok_tikwm(url: str, audio_only: bool = False) -> Optional[dict]:
    """
    Cookie-free TikTok engine via the public tikwm.com API.
    Returns no-watermark links hosted on tikwm's CDN — NOT IP-bound, so both
    extraction and same-instance download work from anywhere (incl. Vercel).
    """
    d = _tikwm_fetch(url)
    if not d:
        return None
    return _tikwm_shape(d, url, audio_only)


# ----------------------------------------------------------------------------
# yt-dlp options per platform
# ----------------------------------------------------------------------------
def ydl_opts(platform: str, audio_only: bool = False,
             custom_format: Optional[str] = None) -> Dict[str, Any]:
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
    if platform == "tiktok":
        opts["format"] = custom_format or ("bestaudio/best" if audio_only else "best")
    elif platform == "instagram":
        cf = _cookies_file(INSTAGRAM_COOKIES, "ig_cookies.txt")
        if cf:
            opts["cookiefile"] = cf
        opts["format"] = custom_format or ("bestaudio/best" if audio_only else "best")
    elif platform == "youtube":
        # yt-dlp is the FALLBACK here (toolz-ytapi is primary, see
        # extract_v1_sync). On a serverless Python runtime there is no JS
        # runtime for yt-dlp's signature/n challenge solver, so only clients
        # that serve plain URLs work: android_vr needs neither a PO token nor
        # a JS runtime. The old ["android", "web"] pair could never produce a
        # stream (android now demands a PO token; web is SABR-only).
        cf = _cookies_file(YOUTUBE_COOKIES, "yt_cookies.txt")
        if cf:
            opts["cookiefile"] = cf
        # Fallback chain, not just "best": many current videos have NO muxed
        # file (DASH-split only), where bare "best" hard-fails with
        # "Requested format is not available" before info is even returned.
        opts["format"] = custom_format or (
            "bestaudio/best" if audio_only else "best/bestvideo+bestaudio")
        opts["extractor_args"] = {"youtube": {"player_client": ["android_vr", "tv", "mweb"]}}
    return opts


def run_ydl(opts: Dict[str, Any], url: str) -> dict:
    import yt_dlp
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


# ----------------------------------------------------------------------------
# Response shaping
# ----------------------------------------------------------------------------
def normalize(info: dict) -> Tuple[List[dict], List[dict]]:
    video, audio = [], []
    for f in info.get("formats") or []:
        if not f.get("url"):
            continue
        # Skip HLS manifests — they're playlists, not downloadable files
        proto = f.get("protocol") or ""
        if "m3u8" in proto or f.get("ext") == "m3u8" or ".m3u8" in f["url"]:
            continue
        # Skip fragmented/DASH entries — segment lists, not single files.
        # Without ffmpeg they can't be served as downloads, so listing them lies.
        if f.get("fragments"):
            continue
        # Skip watermarked variants — everything served here is no-watermark.
        note = f"{f.get('format_note') or ''} {f.get('format_id') or ''}".lower()
        if "watermark" in note:
            continue
        fmt = {
            "format_id": f.get("format_id"), "ext": f.get("ext"),
            "resolution": f.get("resolution") or f.get("format_note") or "unknown",
            "url": f["url"],
            "filesize": f.get("filesize"),  # exact only — approximations are never shown
            "vcodec": f.get("vcodec"), "acodec": f.get("acodec"),
            "has_audio": f.get("has_audio"),  # explicit when the source states it, else null
            "height": f.get("height"), "width": f.get("width"),
            "tbr": f.get("tbr"), "abr": f.get("abr"), "fps": f.get("fps"),
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
    return _dedupe(video, ("width", "height", "vcodec", "acodec")), _dedupe(audio, ("acodec", "abr"))


def _dedupe(rows: List[dict], keys: Tuple[str, ...]) -> List[dict]:
    """Collapse same-spec rows (IG duplicate encodes, TikTok -0/-1 pairs).

    Keeps the entry with a known filesize, else highest bitrate, else first.
    """
    best: Dict[tuple, dict] = {}

    def score(r):
        return (1 if r.get("filesize") else 0, r.get("tbr") or r.get("abr") or 0)

    for r in rows:
        k = tuple(r.get(c) for c in keys)
        if k not in best or score(r) > score(best[k]):
            best[k] = r
    ordered = sorted(best.values(), key=lambda r: rows.index(r))
    return ordered


def prefer_1080(rows: List[dict]) -> List[dict]:
    """1080p first when present, otherwise keep existing (highest-first) order."""
    exact = [r for r in rows if (r.get("height") or 0) == 1080]
    return (exact + [r for r in rows if r not in exact]) if exact else rows


def shape(platform: str, info: dict, original_url: str) -> dict:
    video, audio = normalize(info)
    dl = info.get("url")
    hdrs = dict(info.get("http_headers") or {})
    dl_cookies = None
    if not dl and info.get("requested_formats"):
        req = info["requested_formats"]
        # A split merge pair (video-only + audio-only rows) is not a
        # downloadable file — ignore it and rank the ladder below, so
        # DASH-only videos get honest rows (and an honest default) instead
        # of a silent video track or a "format not available" abort.
        # Single preselected files (TikTok/IG best) pass through untouched.
        is_split_pair = len(req) > 1 and any(
            f.get("vcodec") == "none" or f.get("acodec") == "none" for f in req)
        if not is_split_pair:
            wanted = [f for f in req
                      if f.get("vcodec") != "none" and "m3u8" not in (f.get("protocol") or "")
                      and ".m3u8" not in (f.get("url") or "")] or req
            pick = wanted[0]
            dl = pick.get("url")
            hdrs = dict(pick.get("http_headers") or {})
            dl_cookies = pick.get("cookies")
    if not dl:
        merged = [f for f in video if f["vcodec"] not in (None, "none") and f["acodec"] not in (None, "none")]
        # Default is 1080p: exact match wins, else highest available.
        pick = prefer_1080(merged or video or audio)
        if pick:
            dl, hdrs = pick[0]["url"], dict(pick[0]["headers"] or {})
    stats = {k: info.get(k) for k in
             ("view_count", "like_count", "comment_count")}
    # Pair the chosen download_url with ITS OWN cookies (TikTok tt_chain_token
    # must match the ?tk= param inside that exact URL or CDN returns 403)
    if dl and not dl_cookies:
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


def gallery_shape(platform: str, info: dict, original_url: str) -> Optional[dict]:
    """Normalize playlist/carousel entries into ordered, individually downloadable assets."""
    entries = [entry for entry in (info.get("entries") or []) if isinstance(entry, dict)]
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
        ext = entry.get("ext") or urllib.parse.urlparse(media).path.rsplit(".", 1)[-1] or "jpg"
        gallery.append({
            "format_id": f"gallery:{index}", "url": media, "headers": headers, "cookies": cookies,
            "ext": ext.lower(), "filesize": entry.get("filesize"), "width": entry.get("width"),
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


def tiktok_gallery_shape(data: dict, url: str) -> Optional[dict]:
    images = [image for image in (data.get("images") or []) if isinstance(image, str) and image.startswith("http")]
    if not images:
        return None
    author = data.get("author") or {}
    # Slideshows are posted with a soundtrack; offer it as its own audio download.
    music = [{"format_id": "tikwm_music", "ext": "mp3", "resolution": "audio",
              "url": data["music"], "filesize": None, "vcodec": "none", "acodec": None,
              "has_audio": False, "ladder": "fast", "ip_free": True,
              "height": None, "tbr": None, "abr": None,
              "headers": dict(BASE_HEADERS), "cookies": None}] if data.get("music") else []
    gallery = [{
        "format_id": f"gallery:{index}", "url": image, "headers": dict(BASE_HEADERS), "cookies": None,
        "ext": urllib.parse.urlparse(image).path.rsplit(".", 1)[-1].lower() or "jpg", "filesize": None,
        "width": None, "height": None, "thumbnail": image, "mime_type": None,
    } for index, image in enumerate(images)]
    return {
        "platform": "tiktok", "title": data.get("title"), "thumbnail": data.get("cover") or gallery[0]["thumbnail"],
        "duration": None, "uploader": author.get("nickname"), "uploader_url": None,
        "stats": {"view_count": _num(data.get("play_count")), "like_count": _num(data.get("digg_count")), "comment_count": _num(data.get("comment_count"))},
        "upload_date": None, "description": None, "download_url": gallery[0]["url"],
        "download_headers": dict(BASE_HEADERS), "download_cookies": None, "ext": gallery[0]["ext"],
        "blocked": False, "formats": {"video": [], "audio": music}, "gallery": gallery, "original_url": url,
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
    "tiktok": ("TikTok extraction failed on this server IP. "
               "Metadata below is from TikTok's public oEmbed. Try again in a minute."),
}


# ----------------------------------------------------------------------------
# Core extraction orchestration
# ----------------------------------------------------------------------------
def tiktok_ladder(url: str, audio_only: bool, fast: dict) -> dict:
    """
    Merge exact yt-dlp rungs into a fast tikwm result (on-demand full ladder).

    Exact rows supersede the approximate tikwm video rows (same files, observed
    specs) — no duplicate 1080p entries. The default download stays the IP-free
    tikwm HD file. Failures degrade to the fast result with ladder_error set.
    """
    try:
        info = run_ydl(ydl_opts("tiktok", audio_only), tiktok_canonical(url))
    except Exception as e:
        out = dict(fast)
        out["ladder"] = "fast"
        out["ladder_error"] = str(e).replace("ERROR: ", "")[:160]
        return out
    video, audio = normalize(info)
    for r in video + audio:
        r["ladder"] = "full"
        r["ip_free"] = False
    out = dict(fast)
    fast_audio = fast.get("formats", {}).get("audio", [])
    if fast_audio:
        # tikwm already serves the sound (IP-free) — the yt-dlp audio row is
        # the same track on a signed CDN, not a real choice.
        merged_audio = fast_audio
    else:
        seen = {(a.get("acodec"), a.get("abr")) for a in fast_audio}
        merged_audio = fast_audio + [a for a in audio if (a.get("acodec"), a.get("abr")) not in seen]
    out["formats"] = {
        "video": video[:20] or fast.get("formats", {}).get("video", []),
        "audio": merged_audio[:10],
    }
    if video:
        out["ladder"] = "full"
    else:
        out["ladder"] = "fast"
        out["ladder_error"] = "Full ladder unavailable — showing fast qualities."
    return out


def extract_sync(url: str, audio_only: bool = False, custom_format: Optional[str] = None) -> dict:
    platform = detect_platform(url)
    if not platform:
        raise ValueError("Unsupported URL. Only TikTok and Instagram are supported.")

    if platform == "tiktok":
        # tikwm first: ~1s, HD no-watermark, IP-free CDN. The full yt-dlp
        # ladder is then merged in automatically — fast enough to be default
        # and exact enough to replace the approximate fast rows.
        # (yt-dlp alone is the fallback: its anti-bot hangs/flags server IPs
        # often, which previously burned the whole EXTRACT_TIMEOUT budget.)
        raw = _tikwm_fetch(url)
        if raw is not None:
            if raw.get("images") and not (raw.get("hdplay") or raw.get("play") or raw.get("wmplay")):
                gallery = tiktok_gallery_shape(raw, url)
                if gallery:
                    return gallery
                raise UnsupportedMedia("This TikTok photo post did not expose downloadable images.")
            shaped = _tikwm_shape(raw, url, audio_only)
            if shaped:
                if not audio_only:
                    return tiktok_ladder(url, audio_only, shaped)
                return shaped
            # Payload parsed but unusable (no media, no images) — fall through to yt-dlp.
        last = None
        candidates = list(dict.fromkeys([url, tiktok_canonical(url)]))
        for _attempt in range(2):
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

    # Instagram reels / posts. yt-dlp represents carousel posts as playlist
    # entries, which become ordered individual assets rather than a fake video.
    try:
        info = run_ydl(ydl_opts("instagram", audio_only, custom_format), url)
        return gallery_shape(platform, info, url) or shape(platform, info, url)
    except Exception as e:
        msg = str(e).replace("ERROR: ", "").split(" Check if this post")[0].strip()
        hint = ("Instagram requires login for most content on server IPs. "
                "Set INSTAGRAM_COOKIES env (Netscape cookies.txt from a logged-in browser) and redeploy.")
        raise RuntimeError(f"{msg[:250]} ({hint})")


YT_TICKET_TTL = 120       # seconds a signed download link stays valid
YT_BUDGET = 50            # whole YouTube extraction (ytapi, then yt-dlp fallback)
YT_FALLBACK_AFTER = 25    # don't start the yt-dlp fallback after this many seconds
_YT_VERDICTS = ("private", "age", "region", "unavailable")


def _ytapi_fetch(url: str) -> dict:
    """Call the extraction microservice (contract v2). Raises RuntimeError on any failure."""
    payload = json.dumps({"url": url, "v": 2}).encode()
    req = urllib.request.Request(
        YT_EXTRACT_URL + "/api/extract",
        data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {YTAPI_SECRET}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=32) as res:
            body = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # The service answers failures as JSON with a structured verdict.
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            raise RuntimeError(f"extraction service unreachable: HTTP {e.code}")
    except Exception as e:
        raise RuntimeError(f"extraction service unreachable: {str(e)[:120]}")
    if not body.get("ok"):
        raise RuntimeError(
            f"extraction service: {body.get('error', 'failed')}: "
            f"{str(body.get('detail') or '')[:900]}"
        )
    data = body.get("data") or {}
    if not data.get("formats"):
        raise RuntimeError("extraction service returned no streams")
    return data


def youtube_shape(contract: dict, url: str, audio_only: bool = False) -> dict:
    """
    Build our response from the microservice contract (v2). Pure (no I/O).

    Rows are `kind`-classified by the service: merged (video+audio muxed at
    download time), progressive (one file with sound), video (silent adaptive
    track) and audio. Silent video-only rows are listed ONLY for heights that
    have no with-sound alternative, and default selection always prefers a
    with-sound row — never a silent track when sound exists.

    The `url` on every row is an opaque sentinel (`ytapi://<id>/<format>`):
    googlevideo URLs are bound to the microservice's egress IP, so this API
    never holds or fetches them. Downloads are redirected to the service
    with a signed ticket (see _ytapi_download_url).
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
            "url": f"ytapi://{vid}/{fid}",
            "filesize": r.get("content_length"),
            "vcodec": "none" if is_audio else (None if r.get("kind") == "merged" else codec),
            "acodec": codec if is_audio else None,
            "has_audio": False if is_audio else bool(r.get("has_audio")),
            "height": r.get("height"), "width": r.get("width"),
            "tbr": (bitrate / 1000) if bitrate else None,
            "abr": (bitrate / 1000) if (is_audio and bitrate) else None,
            "fps": r.get("fps"),
            "mime_type": mime.split(";")[0].strip() or None,
            "headers": {}, "cookies": None,
        }
        (audio if is_audio else video).append(row)
    if not video and not audio:
        raise RuntimeError("extraction service returned no streams")

    sounded = {v["height"] for v in video if v["has_audio"]}
    video = [v for v in video if v["has_audio"] or v["height"] not in sounded]
    video.sort(key=lambda v: (v["height"] or 0, v["fps"] or 0, 1 if v["has_audio"] else 0,
                              v["tbr"] or 0), reverse=True)
    audio.sort(key=lambda a: (a["abr"] or 0, 1 if a["ext"] == "m4a" else 0), reverse=True)

    with_sound = [v for v in video if v["has_audio"]]
    if audio_only and audio:
        best, best_group = audio[0], "audio"
    elif video:
        best, best_group = prefer_1080(with_sound or video)[0], "video"
    else:
        best, best_group = audio[0], "audio"

    stats = {"view_count": contract.get("view_count"), "like_count": contract.get("like_count"),
             "comment_count": contract.get("comment_count")}
    return {
        "platform": "youtube", "source": "ytapi", "video_id": vid,
        "title": contract.get("title"), "thumbnail": contract.get("thumbnail"),
        "duration": contract.get("duration"), "uploader": contract.get("uploader"),
        "uploader_url": contract.get("uploader_url"), "stats": stats,
        "upload_date": contract.get("upload_date"),
        "description": (contract.get("description") or "")[:400] or None,
        "download_url": None, "download_headers": {}, "download_cookies": None,
        "ext": best["ext"], "blocked": False,
        "best_format": best["format_id"], "best_group": best_group,
        "formats": {"video": video[:20], "audio": audio[:10]},
        "original_url": url,
    }


def _ytapi_fatal(msg: str) -> bool:
    """A definitive verdict from the service — falling back to yt-dlp can't change it."""
    m = re.search(r"extraction service:\s*(\w+):", msg or "")
    return bool(m and m.group(1) in _YT_VERDICTS)


def _ytapi_primary_error(ytapi_msg: str, ytdl_msg: str) -> RuntimeError:
    """Prefer the microservice's structured verdict over yt-dlp wall text."""
    m = re.search(r"extraction service:\s*(\w+):\s*(.*)", ytapi_msg)
    if not m:
        return RuntimeError(f"YouTube extraction failed: {ytdl_msg[:200]} [{ytapi_msg[:200]}]")
    code, detail = m.group(1), m.group(2).strip()
    if code == "private":
        return RuntimeError("This YouTube video is private.")
    if code == "age":
        return RuntimeError(f"This YouTube video is age-restricted ({detail[:120]}).")
    if code == "region":
        return RuntimeError(f"This YouTube video is blocked in this region ({detail[:120]}).")
    if code == "unavailable":
        return RuntimeError(
            "This YouTube video doesn't exist, is private, or was removed. "
            f"Check the link and try a public video. ({detail[:120]})")
    if code == "login":
        return RuntimeError(
            "YouTube demanded sign-in for this video even with session cookies — "
            "re-export YOUTUBE_COOKIES from the throwaway account and redeploy. "
            f"({detail[:120]})"
        )
    tail = f" | yt-dlp: {ytdl_msg[:160]}" if ytdl_msg else ""
    return RuntimeError(f"YouTube extraction failed: [{code}] {detail[:900]}{tail}")


def _ytapi_download_url(video_id: str, format_id: str, filename: str,
                        height: Optional[int] = None) -> str:
    """
    Signed, short-lived link to the microservice's streaming endpoint.

    googlevideo URLs only work from the IP that extracted them, so the one
    process that may download is the microservice itself — it re-extracts
    inside the download request and streams. Wire format matches
    toolz-ytapi/lib/ticket.js: b64url(JSON) "." b64url(HMAC-SHA256).
    """
    if not (YT_EXTRACT_URL and YTAPI_SECRET):
        raise HTTPException(503, "YouTube downloads need YT_EXTRACT_URL and YTAPI_SECRET to be configured.")
    payload = {"v": video_id, "f": format_id, "n": filename, "e": int(time.time()) + YT_TICKET_TTL}
    if height:
        payload["h"] = int(height)
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(
        hmac.new(YTAPI_SECRET.encode(), body.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return f"{YT_EXTRACT_URL}/api/extract?t={body}.{sig}"


def extract_v1_sync(url: str, audio_only: bool = False) -> dict:
    """Versioned extraction entry point. Legacy routes remain TikTok/IG-only."""
    platform = detect_v1_platform(url)
    if platform == "youtube":
        started = time.monotonic()
        ytapi_msg = ""
        if YT_EXTRACT_URL:
            # Primary: youtubei.js service. It verifies every stream against
            # googlevideo from the IP that will later download it, so a
            # result is never "formats that 403".
            try:
                return youtube_shape(_ytapi_fetch(url), url, audio_only)
            except RuntimeError as e:
                ytapi_msg = str(e)
            except Exception as e:  # defensive: shaping bugs must not mask the fallback
                ytapi_msg = f"extraction service: failed: {str(e)[:160]}"
            if _ytapi_fatal(ytapi_msg):
                raise _ytapi_primary_error(ytapi_msg, "")
            if time.monotonic() - started > YT_FALLBACK_AFTER:
                raise _ytapi_primary_error(ytapi_msg, "skipped: time budget")
        try:
            info = run_ydl(ydl_opts("youtube", audio_only), url)
            return gallery_shape(platform, info, url) or shape(platform, info, url)
        except Exception as exc:
            msg = str(exc).replace("ERROR: ", "")
            if ytapi_msg:
                raise _ytapi_primary_error(ytapi_msg, msg)
            if "Requested format is not available" in msg or "no video formats" in msg.lower():
                # yt-dlp found zero usable streams. Distinguish a dead link
                # (oEmbed 404s too) from a genuine extraction refusal so the
                # UI can say so instead of quoting yt-dlp internals.
                if not youtube_oembed(url):
                    raise RuntimeError(
                        "This YouTube video doesn't exist, is private, or was "
                        "removed. Check the link and try a public video."
                    )
            raise RuntimeError(f"YouTube extraction failed: {msg[:320]}")
    return extract_sync(url, audio_only)


# ----------------------------------------------------------------------------
# Auth / validation helpers
# ----------------------------------------------------------------------------
def client_ip(request: Request) -> str:
    """Real visitor IP behind Vercel's proxy (else the rate limit is global)."""
    xff = request.headers.get("x-forwarded-for") or ""
    if xff:
        return xff.split(",")[0].strip() or "anon"
    return request.client.host if request.client else "anon"


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


def _guest_key(token: str) -> str:
    return "downloadz:v1:guest:" + hashlib.sha256(token.encode()).hexdigest()


def _extraction_key(extraction_id: str) -> str:
    return "downloadz:v1:extraction:" + extraction_id


def issue_guest_session(installation_id: str, request: Request) -> dict:
    installation_id = (installation_id or "").strip()
    if len(installation_id) < 16 or len(installation_id) > 128:
        raise HTTPException(status_code=400, detail="installation_id must be 16–128 characters")
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    state_set(_guest_key(token), {
        "installation_hash": hashlib.sha256(installation_id.encode()).hexdigest(),
        "ip": client_ip(request), "created_at": now, "expires_at": now + GUEST_SESSION_TTL,
    }, GUEST_SESSION_TTL)
    return {"access_token": token, "token_type": "Bearer", "expires_in": GUEST_SESSION_TTL}


def require_guest_session(request: Request) -> Tuple[str, dict]:
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="A guest session bearer token is required")
    token = auth[7:].strip()
    if len(token) < 32:
        raise HTTPException(status_code=401, detail="Invalid guest session")
    session = state_get(_guest_key(token))
    if not session or int(session.get("expires_at", 0)) <= time.time():
        raise HTTPException(status_code=401, detail="Guest session expired. Create a new session.")
    return token, session


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


_FFMPEG: Optional[str] = ""  # "" = not looked up yet, None = unavailable


def ffmpeg_path() -> Optional[str]:
    """
    Optional ffmpeg for merging split tracks. Order: FFMPEG_PATH env, the
    `imageio-ffmpeg` wheel (bundles a static binary), then PATH. Absent means
    split-track merging is simply not offered — nothing else changes.
    """
    global _FFMPEG
    if _FFMPEG != "":
        return _FFMPEG
    cand = (os.getenv("FFMPEG_PATH") or "").strip() or None
    if not cand:
        try:
            import imageio_ffmpeg  # type: ignore
            cand = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            cand = shutil.which("ffmpeg")
    _FFMPEG = cand if cand and os.path.isfile(cand) and os.access(cand, os.X_OK) else None
    return _FFMPEG


def _mux_pairs(result: dict) -> List[dict]:
    """
    Synthetic "video + audio" rows for yt-dlp sources that only offer split
    tracks at their top qualities (Instagram DASH, YouTube fallback). Only
    built when ffmpeg exists, only for heights with no with-sound row, one
    per (height, container), always paired with the best same-family audio
    so stream-copy muxing is legal. v1 only: legacy routes never see them.
    """
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
    sounded = {v.get("height") for v in video
               if v.get("has_audio") is True or v.get("acodec") not in (None, "none")}
    out: List[dict] = []
    seen = set()
    for v in video:
        if not v.get("url") or v.get("acodec") != "none" or not v.get("height") or v["height"] in sounded:
            continue
        cont = "webm" if ext(v) == "webm" else "mp4"
        if not pools[cont] or (v["height"], cont) in seen:
            continue
        seen.add((v["height"], cont))
        a = pools[cont][0]  # lists arrive sorted best-first
        both = v.get("filesize") and a.get("filesize")
        out.append({
            "format_id": f"mux:{v['format_id']}+{a['format_id']}", "ext": cont,
            "resolution": v.get("resolution"), "url": v["url"],
            "filesize": (v["filesize"] + a["filesize"]) if both else None,
            "vcodec": v.get("vcodec"), "acodec": a.get("acodec"), "has_audio": True,
            "height": v["height"], "width": v.get("width"), "tbr": v.get("tbr"), "fps": v.get("fps"),
            "headers": {}, "cookies": None, "mux": True,
            "_video": v, "_audio": a,  # internal: never copied into assets
        })
    return out


def make_v1_extraction(source_url: str, result: dict, owner_installation_hash: str) -> dict:
    """Persist private upstream details but return only opaque asset handles."""
    extraction_id = secrets.token_urlsafe(18)
    asset_map: Dict[str, dict] = {}
    best = {
        "ext": result.get("ext"), "filesize": None, "duration": result.get("duration"),
        "has_audio": not bool(result.get("gallery")), "thumbnail": result.get("thumbnail"),
    }
    best_kind, best_source = "video", "best"
    best_id = result.get("best_format")
    if best_id:
        # Sources that pick their own default (YouTube) describe it truthfully
        # instead of a generic "best" with unknown properties.
        for group in ("video", "audio"):
            row = next((f for f in result.get("formats", {}).get(group, [])
                        if str(f.get("format_id")) == str(best_id)), None)
            if row:
                best.update({k: row.get(k) for k in ("ext", "filesize", "width", "height",
                                                     "resolution", "vcodec", "acodec", "mime_type")})
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
        if not item.get("url"):
            continue
        asset_id = f"i_{index}"
        asset_map[asset_id] = _asset_record(asset_id, "image", item, str(item.get("format_id") or f"gallery:{index}"))
    # A gallery has no meaningful generic "best" file; clients receive only
    # its ordered images and cannot accidentally download the first item twice.
    if result.get("gallery"):
        asset_map.pop("best", None)
    now = int(time.time())
    record = {
        "id": extraction_id, "owner": owner_installation_hash,
        "created_at": now, "expires_at": now + EXTRACTION_TTL, "source_url": source_url,
        "result": result, "assets": asset_map,
    }
    state_set(_extraction_key(extraction_id), record, EXTRACTION_TTL)
    return record


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


def clean_url(url: str) -> str:
    url = (url or "").strip()
    if not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="URL must start with https://")
    if len(url) > 2048:
        raise HTTPException(status_code=400, detail="URL too long")
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="Invalid source URL")
    if host in ("localhost",) or host.endswith((".local", ".internal")):
        raise HTTPException(status_code=400, detail="Private/internal hosts are not allowed")
    try:
        address = ipaddress.ip_address(host)
        if not address.is_global:
            raise HTTPException(status_code=400, detail="Private/internal hosts are not allowed")
    except ValueError:
        pass
    return url


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------
app = FastAPI(
    title="toolz-downloadz-api",
    description="Media extraction for TikTok and Instagram",
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
    # Do not leak provider URLs, cookie failures, or implementation details to
    # public clients. Vercel captures this structured log for alerting.
    print(json.dumps({"event": "unhandled_error", "path": request.url.path, "type": type(exc).__name__}))
    return JSONResponse(status_code=500, content={"detail": "The download service encountered an unexpected error. Please retry."})


@app.get("/api/health")
async def health():
    return {
        "status": "online",
        "service": "toolz-downloadz-api",
        "version": VERSION,
        "platforms": SUPPORTED,
        "api_versions": ["legacy", "v1"],
        "state": "upstash-redis" if REDIS_REST_URL and REDIS_REST_TOKEN else "local-development",
    }


@app.get("/")
async def root():
    return {"name": "toolz-downloadz-api", "version": VERSION, "docs": "/docs", "health": "/api/health"}


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


async def do_extract(request: Request, url: str, audio_only: bool, custom_format: Optional[str]):
    ident = client_ip(request)
    if not rate_ok(ident):
        raise HTTPException(status_code=429, detail=f"Rate limit exceeded ({RATE_LIMIT}/min). Slow down.")
    check_auth(request)
    url = clean_url(url)
    platform = detect_platform(url)
    if not platform:
        raise HTTPException(status_code=400, detail="Unsupported URL. Only TikTok and Instagram are supported.")

    key = _ckey(_key_url(url), f"{audio_only}|{custom_format}")
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
async def extract_post(request: Request, body: Optional[Dict[str, Any]] = None):
    if body is None:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
    url = (body or {}).get("url")
    if not url:
        raise HTTPException(status_code=400, detail="Missing 'url' in body")
    return await do_extract(request, url, bool(body.get("audio_only")), body.get("format"))


# ----------------------------------------------------------------------------
# v1 public client contract.  Unlike the legacy endpoints, it never returns
# upstream media URLs, cookies, headers, or a project-wide API secret.
# ----------------------------------------------------------------------------
@app.post("/api/v1/client-sessions")
async def create_client_session(request: Request, body: Optional[Dict[str, Any]] = None):
    if not rate_ok("session:" + client_ip(request)):
        raise HTTPException(status_code=429, detail="Too many session requests. Try again shortly.")
    if body is None:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
    return issue_guest_session(str((body or {}).get("installation_id") or ""), request)


@app.post("/api/v1/extractions")
async def create_v1_extraction(request: Request, body: Optional[Dict[str, Any]] = None):
    token, session = require_guest_session(request)
    if not rate_ok("extract:" + hashlib.sha256(token.encode()).hexdigest()[:16]):
        raise HTTPException(status_code=429, detail="Too many extraction requests. Wait a minute and retry.")
    if body is None:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
    payload = body or {}
    url = clean_url(str(payload.get("url") or ""))
    platform = detect_v1_platform(url)
    if not platform:
        raise HTTPException(status_code=400, detail="Unsupported URL. Use a public TikTok, Instagram, or YouTube link.")
    audio_only = bool(payload.get("audio_only"))
    try:
        loop = asyncio.get_running_loop()
        # YouTube may chain yt-dlp + microservice + token minting on cold
        # starts; give it room inside the 60s function budget. TikTok/IG
        # keep the tight default.
        budget = max(EXTRACT_TIMEOUT, 50) if platform == "youtube" else max(EXTRACT_TIMEOUT, 5)
        result = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: extract_v1_sync(url, audio_only)),
            timeout=budget,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Extraction timed out. Retry in a moment.")
    except (RuntimeError, ValueError, UnsupportedMedia) as exc:
        raise HTTPException(status_code=422, detail=str(exc)[:1100])
    if result.get("blocked"):
        raise HTTPException(status_code=422, detail=result.get("blocked_message") or "This media is unavailable.")
    cache_set(_ckey(_key_url(url), "True|False|None"), result)
    return public_v1_extraction(make_v1_extraction(url, result, str(session["installation_hash"])))


@app.get("/api/v1/extractions/{extraction_id}")
async def get_v1_extraction(extraction_id: str, request: Request):
    _token, session = require_guest_session(request)
    record = state_get(_extraction_key(extraction_id))
    if not record or record.get("owner") != session.get("installation_hash"):
        raise HTTPException(status_code=404, detail="Extraction expired or was not found.")
    return public_v1_extraction(record)


@app.get("/api/v1/extractions/{extraction_id}/assets/{asset_id}/download")
async def download_v1_asset(extraction_id: str, asset_id: str, request: Request):
    _token, session = require_guest_session(request)
    record = state_get(_extraction_key(extraction_id))
    if not record or record.get("owner") != session.get("installation_hash"):
        raise HTTPException(status_code=404, detail="Download expired. Extract the link again.")
    asset = (record.get("assets") or {}).get(asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="The selected format is no longer available.")
    title = _sanitize_name(record.get("result", {}).get("title") or "media")
    suffix = f"-{asset_id}" if asset.get("kind") == "image" else ""
    filename = f"{title}{suffix}.{asset.get('ext') or 'mp4'}"
    result = record.get("result") or {}
    if result.get("source") == "ytapi":
        # googlevideo bytes may only be fetched by the process that extracted
        # them (IP-bound): hand the client a signed ticket to that service.
        return RedirectResponse(
            _ytapi_download_url(str(result.get("video_id") or ""), str(asset["format"]),
                                filename, asset.get("height")),
            status_code=307, headers={"Cache-Control": "no-store"},
        )
    return await _stream_download(
        request, record["source_url"], str(asset["format"]), filename,
        str(result.get("platform") or ""), v1=True,
    )


# ----------------------------------------------------------------------------
# One-step download: resolve formats + stream media FROM THE SAME LAMBDA.
# Critical because TikTok signs media URLs to the requesting IP — a
# separate proxy server gets 403. Same-instance fetch keeps the signature valid.
# ----------------------------------------------------------------------------
def _sanitize_name(name: str) -> str:
    keep = "".join(c if (c.isalnum() or c in " ._-") else " " for c in name)
    return (" ".join(keep.split()) or "media")[:120]


async def _mux_download(resolve, format_id: str, filename: str):
    """Stream a merged video+audio file: ffmpeg stream-copies two tracks, no re-encode, no temp file."""
    plan = None
    for fresh in (False, True):
        result = await resolve(fresh)
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
        hdrs = dict(fmt.get("headers") or {})
        if fmt.get("cookies"):
            hdrs["Cookie"] = fmt["cookies"]
        if hdrs:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())]
        cmd += ["-i", fmt["url"]]
    cont = plan["ext"]
    cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c", "copy"]
    cmd += ["-f", "webm"] if cont == "webm" else \
        ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4"]
    cmd += ["pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    loop = asyncio.get_running_loop()
    # First bytes decide success: a refusal becomes a clean error, not an empty "download".
    first = await loop.run_in_executor(None, proc.stdout.read, 65536)
    if not first:
        err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()[:160]
        proc.kill()
        raise HTTPException(502, f"Could not merge the video and audio tracks ({err or 'source refused'}). Extract again.")

    def _iter():
        try:
            yield first
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.kill()
            proc.wait()

    return StreamingResponse(_iter(), media_type="video/webm" if cont == "webm" else "video/mp4", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(_sanitize_name(filename))}",
        "Cache-Control": "no-store", "Accept-Ranges": "none",
    })


async def _stream_download(request: Request, page_url: str, f: str, n: str, platform: str, v1: bool = False):
    """Resolve and stream a legacy or v1 asset from the extracting function."""

    # Resolve + stream via explicit strategy chain. Each strategy is one
    # (resolve-mode, media-source) combo; first successful open wins.
    range_header = request.headers.get("range")
    errors: List[str] = []
    resp = None

    async def _resolve(fresh: bool) -> dict:
        key = _ckey(_key_url(page_url), f"{v1}|False|None")
        result = None if fresh else cache_get(key)
        if not result or result.get("blocked"):
            loop = asyncio.get_running_loop()
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: extract_v1_sync(page_url) if v1 else extract_sync(page_url)),
                    timeout=YT_BUDGET if platform == "youtube" else max(EXTRACT_TIMEOUT, 5),
                )
            except asyncio.TimeoutError:
                raise HTTPException(504, f"Preparing the download timed out (~{EXTRACT_TIMEOUT}s). Tap Download once more — retries usually succeed.")
            except HTTPException:
                raise
            except RuntimeError as e:
                # extract_sync failures (IG cookies, slideshows) are client errors, not 500s
                raise HTTPException(400, str(e)[:300])
            if result.get("blocked"):
                raise HTTPException(409, result.get("blocked_message", "Extraction blocked"))
            cache_set(key, result)
        return result

    def _pick(result):
        headers = dict(result.get("download_headers") or {})
        cookies = result.get("download_cookies")
        found = f == "best" or not f
        if not found:
            for group in ("video", "audio"):
                for fmt in result.get("formats", {}).get(group, []):
                    if str(fmt.get("format_id")) == f:
                        return fmt["url"], dict(fmt.get("headers") or {}), fmt.get("cookies"), True
            for item in result.get("gallery", []):
                if str(item.get("format_id")) == f:
                    return item["url"], dict(item.get("headers") or {}), item.get("cookies"), True
        media = result.get("download_url")
        if media and not headers:
            vids = result.get("formats", {}).get("video") or []
            if vids:
                headers = dict(vids[0].get("headers") or {})
        return media, headers, cookies, found

    def _sync_open(media_url, h):
        req = urllib.request.Request(media_url, headers=h)
        return urllib.request.urlopen(req, timeout=25)

    def _h_with_range(h):
        h2 = dict(h)
        if range_header:
            h2["Range"] = range_header
        return h2

    if f.startswith("mux:"):
        return await _mux_download(_resolve, f, n)

    strategies = [("cached", False), ("fresh", True)]
    for label, fresh in strategies:
        try:
            result = await _resolve(fresh)
        except HTTPException as he:
            if he.status_code == 409 or label == "fresh":
                raise
            errors.append(str(he.detail)[:100])
            continue
        media, hdrs, cookies, found = _pick(result)
        if not found:
            if label == "cached":
                continue  # fresh resolve may list it
            raise HTTPException(status_code=404, detail="That quality is no longer listed. Extract again.")
        if not media:
            continue
        h = dict(hdrs)
        if cookies:
            h["Cookie"] = cookies
        try:
            resp = _sync_open(media, _h_with_range(h))
        except urllib.error.HTTPError as e:
            errors.append(f"{label}:{e.code}")
            # TikTok: yt-dlp CDN URLs can be IP-rejected even when fresh —
            # tikwm's CDN is IP-free. Always available as last resort.
            if platform == "tiktok" and e.code in (403, 410):
                alt = tiktok_tikwm(page_url)
                if alt and alt.get("download_url"):
                    try:
                        resp = _sync_open(alt["download_url"], dict(alt.get("download_headers") or {}))
                        break
                    except Exception as e2:
                        errors.append(f"tikwm:{str(getattr(e2, 'code', e2))[:40]}")
            # Some CDNs require an explicit Range; retry before moving on
            try:
                resp = _sync_open(media, {**_h_with_range(h), "Range": "bytes=0-"})
                break
            except Exception as e2:
                errors.append(str(getattr(e2, "code", e2))[:40])
                continue
        except Exception as e:
            errors.append(str(e)[:80])
            continue
        break

    if resp is None:
        raise HTTPException(
            status_code=502,
            detail=f"Media source refused ({'; '.join(errors[-3:])}). Extract again.",
        )

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


@app.get("/api/download")
async def download(
    request: Request,
    u: str = Query(..., description="Original page URL"),
    f: str = Query("best", description="yt-dlp format_id, or 'best'"),
    n: str = Query("", description="Filename"),
):
    """Legacy downloader. New clients use authenticated v1 asset paths."""
    check_auth(request)  # supports ?key= only for legacy browser navigation
    page_url = clean_url(u)
    platform = detect_platform(page_url)
    if not platform:
        raise HTTPException(status_code=400, detail="Unsupported URL")
    return await _stream_download(request, page_url, f, n, platform)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.index:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
