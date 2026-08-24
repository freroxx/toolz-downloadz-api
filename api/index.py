"""
toolz-downloadz-api v3.0 — single-file, self-contained FastAPI app for Vercel.
Platforms: YouTube, TikTok, Instagram Reels ONLY.

Why single file: Vercel's Python builder is picky about packages inside api/.
A flat, dependency-free-import file eliminates the NOT_FOUND class of bugs.

Run locally:  uvicorn api.index:app --reload   (or: python api/index.py)
"""
import os
import re
import sys
import glob
import shutil
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
POT_PROVIDER_URL = os.getenv("YT_DLP_POT_PROVIDER_URL", "").strip() or os.getenv("POT_PROVIDER_URL", "").strip()

# Hot-reloadable cookie store (Upstash Redis REST / Vercel KV).
# Precedence: KV value (if set) > YOUTUBE_COOKIES env. Lets you refresh
# cookies via POST /api/admin/cookies without a redeploy.
KV_REST_URL = (os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("KV_REST_API_URL") or "").strip()
KV_REST_TOKEN = (os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("KV_REST_API_TOKEN") or "").strip()
COOKIES_KV_KEY = "toolz:yt_cookies"


def _find_node_dir() -> Optional[str]:
    """Locate the node binary (nodejs-wheel-binaries installs one on Vercel)."""
    p = shutil.which("node")
    if p:
        return os.path.dirname(p)
    cands = ["/tmp/_vc_deps/bin/node"]
    for sp in list(sys.path) + [os.getcwd()]:
        cands += glob.glob(os.path.join(sp, "nodejs_wheel", "bin", "node"))
        cands += glob.glob(os.path.join(sp, "nodejs-wheel-binaries", "**", "node"), recursive=True)
        cands += glob.glob(os.path.join(sp, "bin", "node"))
    for c in cands:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return os.path.dirname(c)
    return None


NODE_DIR = _find_node_dir()


def _pot_plugin_installed() -> bool:
    """The bgutil yt-dlp plugin MUST be importable or tokens are never minted."""
    import importlib.util
    return importlib.util.find_spec("yt_dlp_plugins.extractor.getpot_bgutil_http") is not None
EXTRACT_TIMEOUT = int(os.getenv("EXTRACT_TIMEOUT", "25"))    # seconds; fits maxDuration=60
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "30"))              # per minute per key
VERSION = "3.5.1"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
BASE_HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

SUPPORTED = ["youtube", "tiktok", "instagram"]

# None = don't override player_client at all (yt-dlp defaults — best when cookies are set)
YT_CLIENT_STRATEGIES = [
    ["tv_simply", "tv", "web_safari"],
    ["android", "ios"],
    ["web_embedded"],
    None,
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


# --- Cookie lifecycle: analyze, KV hot-reload, gate -------------------------
LOGIN_PROOF = ("SID", "__Secure-1PSID", "LOGIN_INFO")
ROTATING = ("SIDCC", "__Secure-1PSIDCC", "__Secure-3PSIDCC")

_cookie_memo = {"ts": 0.0, "payload": None}  # memoized resolver result (60s)


def _parse_cookies(content: str) -> Dict[str, dict]:
    """Netscape format -> {name: {expiry:int, domain:str, value:str}}"""
    out: Dict[str, dict] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, _path, _secure, expiry, name = parts[:6]
        out[name] = {"expiry": int(expiry) if expiry.isdigit() else 0,
                     "domain": domain, "value": parts[6]}
    return out


def _analyze_cookies(content: str) -> Dict[str, Any]:
    """Verdict engine: fresh | aging | expired | not_logged_in | invalid."""
    if not content or not content.strip():
        return {"verdict": "none_set", "logged_in": False, "days_left": None,
                "expired": [], "hint": "No cookies configured."}
    cookies = _parse_cookies(content)
    names = set(cookies)
    if not names:
        return {"verdict": "invalid", "logged_in": False, "days_left": None,
                "expired": [], "hint": "Content is not Netscape cookie format."}
    logged_in = bool(names & {"SID", "__Secure-1PSID"}) or "LOGIN_INFO" in names
    now = time.time()
    expired, days_left_min = [], None
    rot = [n for n in ROTATING if n in cookies]
    if rot:
        for n in rot:
            left = (cookies[n]["expiry"] - now) / 86400.0
            days_left_min = left if days_left_min is None else min(days_left_min, left)
            if left <= 0:
                expired.append(n)
        dl = round(max(days_left_min, 0), 1)
    else:
        dl = None  # no rotating cookies present — can't judge freshness
    if not logged_in:
        verdict = "not_logged_in"
    elif expired or (dl is not None and dl <= 0):
        verdict = "expired"
    elif dl is not None and dl < 2:
        verdict = "aging"
    else:
        verdict = "fresh"
    hint = {
        "fresh": f"OK — ~{dl}d until rotation.",
        "aging": f"Rotates soon (~{dl}d). Refresh when convenient.",
        "expired": "Session cookies expired — YouTube will bot-block. Re-export.",
        "not_logged_in": "Export lacks login cookies (no SID). Export while logged in.",
        "invalid": "Unparsable content.",
        "none_set": "Not configured.",
    }[verdict]
    return {"verdict": verdict, "logged_in": logged_in, "days_left": dl,
            "expired": expired, "hint": hint}


def _kv_get(key: str) -> Optional[str]:
    if not (KV_REST_URL and KV_REST_TOKEN):
        return None
    try:
        req = urllib.request.Request(
            f"{KV_REST_URL.rstrip('/')}/get/{urllib.parse.quote(key, safe='')}",
            headers={"Authorization": f"Bearer {KV_REST_TOKEN}"})
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        raw = data.get("result")
        return raw if isinstance(raw, str) else None
    except Exception:
        return None


def _kv_set(key: str, value: str) -> bool:
    if not (KV_REST_URL and KV_REST_TOKEN):
        return False
    try:
        req = urllib.request.Request(
            f"{KV_REST_URL.rstrip('/')}/set/{urllib.parse.quote(key, safe='')}",
            data=value.encode("utf-8"),
            headers={"Authorization": f"Bearer {KV_REST_TOKEN}",
                     "Content-Type": "text/plain"})
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        return True
    except Exception:
        return False


def _kv_del(key: str) -> bool:
    if not (KV_REST_URL and KV_REST_TOKEN):
        return False
    try:
        req = urllib.request.Request(
            f"{KV_REST_URL.rstrip('/')}/del/{urllib.parse.quote(key, safe='')}",
            headers={"Authorization": f"Bearer {KV_REST_TOKEN}"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        return True
    except Exception:
        return False


def get_youtube_cookies() -> Dict[str, Any]:
    """
    Effective YouTube cookies with precedence KV > env, gated by freshness.
    Returns {"content": str|None (None = do NOT send), "source", **analysis}.
    Memoized 60s so per-request latency stays zero; admin POST invalidates locally.
    """
    now = time.time()
    if _cookie_memo["payload"] and now - _cookie_memo["ts"] < 60:
        return _cookie_memo["payload"]
    kv = _kv_get(COOKIES_KV_KEY)
    source = "kv" if kv else "env"
    content = kv or YOUTUBE_COOKIES
    analysis = _analyze_cookies(content)
    # Smart gating: never send dead/invalid sessions — they make blocks worse.
    send = analysis["verdict"] in ("fresh", "aging") and analysis["logged_in"] and content
    payload = {"content": content if send else None, "raw_present": bool(content),
               "source": source if content else "none", **analysis}
    _cookie_memo.update({"ts": now, "payload": payload})
    return payload


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


def _yt_video_id(url: str) -> Optional[str]:
    """Extract the 11-char video id from any YouTube URL shape."""
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").replace("www.", "")
    if host == "youtu.be":
        vid = p.path.lstrip("/").split("/")[0]
        return vid or None
    q = urllib.parse.parse_qs(p.query).get("v")
    if q:
        return q[0]
    parts = [s for s in p.path.split("/") if s]
    for i, seg in enumerate(parts):
        if seg in ("shorts", "embed", "v", "live") and i + 1 < len(parts):
            return parts[i + 1]
    m = re.search(r"[a-zA-Z0-9_-]{11}", url)
    return m.group(0) if m else None


def _mint_video_bound_token(video_id: str) -> Optional[str]:
    """
    Mint a PO token bound to the VIDEO ID via our toolz-pot server.
    tv-client tokens bound to content (not visitor) are honored regardless of
    IP reputation — this is the cookie-free Vercel bypass.
    """
    if not POT_PROVIDER_URL:
        return None
    try:
        body = json.dumps({"content_binding": video_id, "bypass_cache": True}).encode()
        req = urllib.request.Request(
            POT_PROVIDER_URL.rstrip("/") + "/get_pot", data=body,
            headers={"User-Agent": UA, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        tok = data.get("poToken")
        return tok or None
    except Exception:
        return None


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


def tiktok_tikwm(url: str) -> Optional[dict]:
    """
    Cookie-free TikTok engine via the public tikwm.com API.
    Returns no-watermark HD links hosted on tikwm's CDN — NOT IP-bound, so both
    extraction and same-instance download work from anywhere (incl. Vercel).
    """
    try:
        api = "https://www.tikwm.com/api/?hd=1&url=" + urllib.parse.quote(tiktok_canonical(url), safe="")
        req = urllib.request.Request(api, headers=BASE_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        if data.get("code") != 0 or not data.get("data"):
            return None
        d = data["data"]
        media = d.get("hdplay") or d.get("play") or d.get("wmplay")
        if not media:
            return None
        video = [{"format_id": "tikwm_hd" if d.get("hdplay") else "tikwm_sd",
                  "ext": "mp4", "resolution": "1080p no-watermark" if d.get("hdplay") else "SD no-watermark",
                  "url": media, "filesize": None, "vcodec": "avc1", "acodec": "mp4a",
                  "height": 1920 if d.get("hdplay") else None, "tbr": None, "abr": None,
                  "headers": dict(BASE_HEADERS), "cookies": None}]
        audio = []
        if d.get("music"):
            audio.append({"format_id": "tikwm_music", "ext": "mp3", "resolution": "audio",
                          "url": d["music"], "filesize": None, "vcodec": "none", "acodec": "mp3",
                          "height": None, "tbr": None, "abr": None,
                          "headers": dict(BASE_HEADERS), "cookies": None})
        author = d.get("author") or {}
        stats_src = d
        def _g(k):
            v = stats_src.get(k)
            try:
                return int(v) if v is not None else None
            except Exception:
                return None
        return {
            "platform": "tiktok",
            "title": d.get("title"),
            "thumbnail": d.get("cover") or d.get("origin_cover"),
            "duration": d.get("duration"),
            "uploader": author.get("nickname"),
            "uploader_url": f"https://www.tiktok.com/@{author.get('unique_id')}" if author.get("unique_id") else None,
            "stats": {"view_count": _g("play_count"), "like_count": _g("digg_count"),
                      "comment_count": _g("comment_count")},
            "upload_date": None, "description": None,
            "download_url": media, "download_headers": dict(BASE_HEADERS),
            "ext": "mp4", "blocked": False, "source": "tikwm",
            "formats": {"video": video, "audio": audio},
            "original_url": url,
        }
    except Exception:
        return None


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
    # EJS: let yt-dlp solve BotGuard/JS challenges with the bundled node
    # (nodejs-wheel-binaries ships node inside the Vercel deps venv).
    if NODE_DIR:
        opts["js_runtimes"] = {"deno": {}, "node": {"path": os.path.join(NODE_DIR, "node")}}
        opts["remote_components"] = ["ejs:npm", "ejs:github"]
    if platform == "youtube":
        yt_ck = get_youtube_cookies()
        cf = _cookies_file(yt_ck["content"], "yt_cookies.txt") if yt_ck["content"] else None
        if cf:
            opts["cookiefile"] = cf
        # Vercel has no ffmpeg → NEVER select merge-pairs ("bv*+ba"). Also note
        # yt-dlp's `best`/`b` only match A+V files; DASH-only sessions need `bv*`.
        # Progressive first, then best pure-video file (UI lists audio separately).
        if POT_PROVIDER_URL:
            # PO-token provider (bgutil) — modern cookie-free YouTube bypass.
            opts["format"] = custom_format or (
                "bestaudio/best" if audio_only
                else "best[vcodec!=none][acodec!=none]/bv*[protocol^=https]/bv*")
            opts["extractor_args"]["youtubepot-bgutilhttp"] = {"base_url": POT_PROVIDER_URL}
        else:
            opts["format"] = custom_format or (
                "bestaudio/best" if audio_only
                else "best[vcodec!=none][acodec!=none][ext=mp4]/bv*[protocol^=https][ext=mp4]/b")
        # Client pinning works with or without POT (plugin mints per-client)
        if yt_clients:
            opts["extractor_args"]["youtube"] = {"player_client": yt_clients}
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
        # Skip HLS manifests — they're playlists, not downloadable files
        proto = f.get("protocol") or ""
        if "m3u8" in proto or f.get("ext") == "m3u8" or ".m3u8" in f["url"]:
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
    dl_cookies = None
    # Merge-selected (POT/DASH): requested_formats holds the pair — prefer VIDEO
    if not dl and info.get("requested_formats"):
        wanted = [f for f in info["requested_formats"]
                  if f.get("vcodec") != "none" and "m3u8" not in (f.get("protocol") or "")
                  and ".m3u8" not in (f.get("url") or "")] or info["requested_formats"]
        pick = wanted[0]
        dl = pick.get("url")
        hdrs = dict(pick.get("http_headers") or {})
        dl_cookies = pick.get("cookies")
    if not dl:
        merged = [f for f in video if f["vcodec"] not in (None, "none") and f["acodec"] not in (None, "none")]
        pick = merged or video or audio
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
        ck_state = get_youtube_cookies()
        last = None
        # Warm the POT provider (cold boot + BotGuard init can take 5-10s;
        # the plugin's own HTTP timeout is shorter than that). We're already
        # inside an executor thread here, so blocking is fine.
        if POT_PROVIDER_URL:
            try:
                urllib.request.urlopen(urllib.request.Request(
                    POT_PROVIDER_URL.rstrip("/") + "/ping", headers={"User-Agent": UA}), timeout=8)
            except Exception:
                pass
        # Full rotation ALWAYS — tv-family first (least bot-walled), plugin
        # mints per-client PO tokens when POT is configured.
        strategy_errors: Dict[str, str] = {}
        # PRIMARY: tv client + video-ID-bound PO token (IP-reputation-immune)
        vid = _yt_video_id(url)
        if POT_PROVIDER_URL and vid:
            tok = _mint_video_bound_token(vid)
            if tok:
                o = ydl_opts("youtube", audio_only, custom_format, ["tv"])
                ea = o.setdefault("extractor_args", {}).setdefault("youtube", {})
                ea["po_token"] = [f"tv.player+{tok}"]
                ea["fetch_pot"] = ["never"]
                try:
                    return shape(platform, run_ydl(o, url), url)
                except Exception as e:
                    last = str(e)
                    strategy_errors["tv+videobound"] = last[:120]

        for clients in YT_CLIENT_STRATEGIES:
            label = "+".join(clients) if clients else "defaults"
            try:
                return shape(platform, run_ydl(ydl_opts("youtube", audio_only, custom_format, clients), url), url)
            except Exception as e:
                last = str(e)
                strategy_errors[label] = last[:120]
                # Try every strategy — tv-family often passes where web was walled.
                continue
        alt = youtube_pytubefix(url)
        if alt:
            return alt
        meta = youtube_oembed(url)
        if meta:
            strat = " | ".join(f"{k}:{v.split(':')[0][:48]}" for k, v in list(strategy_errors.items())[:4])
            diag = f"{(last or 'unknown')[:120]} [{strat}]"
            return blocked_shape(
                "youtube", meta, url,
                BLOCK_MSGS["youtube"] + (
                    f"\n\n[diag: {diag} | pot={'on' if POT_PROVIDER_URL else 'off'} | "
                    f"cookies={ck_state.get('verdict')} | "
                    f"plugin={'on' if _pot_plugin_installed() else 'MISSING — redeploy'}]")
            )
        raise RuntimeError(f"YouTube extraction failed: {(last or 'unknown')[:300]}")

    if platform == "tiktok":
        # tikwm first: ~1s, HD no-watermark, IP-free CDN. yt-dlp is the
        # fallback because TikTok's anti-bot hangs/flags server IPs often,
        # which previously burned the whole EXTRACT_TIMEOUT budget.
        alt = tiktok_tikwm(url)
        if alt:
            return alt
        last = None
        candidates = list(dict.fromkeys([url, tiktok_canonical(url)]))
        for attempt in range(2):
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


def _pot_status() -> Dict[str, Any]:
    if not POT_PROVIDER_URL:
        return {"configured": False, "hint": "Set YT_DLP_POT_PROVIDER_URL in Vercel env"}
    base = POT_PROVIDER_URL.rstrip("/")
    out: Dict[str, Any] = {"configured": True, "base": base}
    t0 = time.time()
    try:
        req = urllib.request.Request(base + "/ping", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            out["ping_status"] = r.status
            out["ping_ms"] = int((time.time() - t0) * 1000)
    except Exception as e:
        out["ping_ms"] = int((time.time() - t0) * 1000)
        out["ping_error"] = str(e)[:200]
        return out
    if out["ping_ms"] > 5000:
        out["warning"] = f"ping took {out['ping_ms']}ms; yt-dlp plugin gives up after 5000ms and skips POT"
    t1 = time.time()
    try:
        req = urllib.request.Request(
            base + "/get_pot",
            data=json.dumps({"bypass_cache": False}).encode(),
            headers={"User-Agent": UA, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            body = json.loads(r.read().decode("utf-8", "ignore"))
            out["get_pot_ms"] = int((time.time() - t1) * 1000)
            tok = body.get("poToken") or ""
            out["token_preview"] = (tok[:24] + "…") if tok else None
            out["mint_ok"] = bool(tok)
    except Exception as e:
        out["get_pot_ms"] = int((time.time() - t1) * 1000)
        out["get_pot_error"] = str(e)[:250]
        out["mint_ok"] = False
    return out


@app.get("/api/ytdebug")
async def ytdebug(request: Request, url: str = Query(...)):
    """Run every YouTube strategy and return the per-client error map."""
    check_auth(request)
    clean = clean_url(url)
    if detect_platform(clean) != "youtube":
        raise HTTPException(status_code=400, detail="Not a YouTube URL")
    results = []
    vid = _yt_video_id(clean)
    if POT_PROVIDER_URL and vid:
        tok = _mint_video_bound_token(vid)
        if tok:
            o = ydl_opts("youtube", False, None, ["tv"])
            ea = o.setdefault("extractor_args", {}).setdefault("youtube", {})
            ea["po_token"] = [f"tv.player+{tok}"]
            ea["fetch_pot"] = ["never"]
            try:
                info = run_ydl(o, clean)
                results.append({"strategy": "tv+videobound", "ok": True,
                                "formats": len((info or {}).get("formats") or [])})
            except Exception as e:
                results.append({"strategy": "tv+videobound", "ok": False,
                                "error": str(e).replace("ERROR: ", "")[:160]})
    for clients in YT_CLIENT_STRATEGIES:
        label = "+".join(clients) if clients else "defaults"
        try:
            info = run_ydl(ydl_opts("youtube", False, None, clients), clean)
            fmts = len((info or {}).get("formats") or [])
            results.append({"strategy": label, "ok": True, "formats": fmts})
        except Exception as e:
            msg = str(e).replace("ERROR: ", "")[:160]
            results.append({"strategy": label, "ok": False,
                            "bot_block": ("Sign in to confirm" in str(e)) or ("not a bot" in str(e)),
                            "error": msg})
    pyt = youtube_pytubefix(clean)
    results.append({"strategy": "pytubefix", "ok": bool(pyt),
                    "note": None if pyt else "no streams"})
    pot = _pot_status()
    ck = get_youtube_cookies()
    return {"url": clean, "results": results,
            "pot": {"configured": bool(POT_PROVIDER_URL), "plugin_installed": _pot_plugin_installed(), **{k: pot.get(k) for k in ("ping_ms", "mint_ok", "get_pot_error", "warning") if k in pot}},
            "cookies": {k: v for k, v in ck.items() if k != "content"}}


@app.get("/api/potcheck")
async def potcheck(request: Request):
    check_auth(request)
    return _pot_status()


@app.get("/api/diag")
async def diag(request: Request):
    """Cookie doctor + POT status. Auth-protected (reveals session metadata)."""
    check_auth(request)
    ck = get_youtube_cookies()
    ck_public = {k: v for k, v in ck.items() if k != "content"}
    pot = _pot_status()
    verdict = ck_public.get("verdict")
    action = {
        "fresh": "Nothing to do.",
        "aging": ck_public.get("hint", ""),
        "expired": "Re-export cookies from a logged-in youtube.com browser and POST to /api/admin/cookies.",
        "not_logged_in": "Export was made while logged out. Log in, then re-export.",
        "invalid": "Content is not a valid cookies.txt. Re-export via 'Get cookies.txt LOCALLY'.",
        "none_set": "Set YOUTUBE_COOKIES in Vercel env or POST /api/admin/cookies.",
    }[verdict]
    return {
        "version": VERSION,
        "cookies": {**ck_public,
                    "action": action,
                    "kv_configured": bool(KV_REST_URL and KV_REST_TOKEN)},
        "pot": pot,
        "effective": {"youtube_cookies_sent": bool(ck.get("content")),
                      "instagram_cookies": bool(INSTAGRAM_COOKIES)},
    }


@app.post("/api/admin/cookies")
async def admin_cookies_post(request: Request):
    check_auth(request)
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            body = await request.json()
            content = (body or {}).get("cookies", "")
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
    else:
        content = (await request.body()).decode("utf-8", "ignore")
    analysis = _analyze_cookies(content)
    if analysis["verdict"] in ("invalid", "not_logged_in"):
        raise HTTPException(status_code=400, detail=f"Rejected: {analysis['hint']}")
    if not (KV_REST_URL and KV_REST_TOKEN):
        raise HTTPException(status_code=501,
                            detail="Cookies look valid, but hot-reload needs Vercel KV / Upstash REST. "
                                   "Vercel Dashboard → Storage → Create KV → connect this project. "
                                   "(Or paste into YOUTUBE_COOKIES env + redeploy.)")
    ok = _kv_set(COOKIES_KV_KEY, content)
    if not ok:
        raise HTTPException(status_code=502, detail="KV write failed — check KV_REST_* env vars")
    _cookie_memo.update({"ts": 0.0, "payload": None})  # this lambda refreshes instantly
    days = analysis.get("days_left")
    return {"stored": True, "verdict": analysis["verdict"],
            "logged_in": analysis["logged_in"],
            "days_left": round(days, 1) if isinstance(days, (int, float)) else days,
            "note": "Other lambdas pick it up within 60s."}


@app.delete("/api/admin/cookies")
async def admin_cookies_delete(request: Request):
    check_auth(request)
    _kv_del(COOKIES_KV_KEY)
    _cookie_memo.update({"ts": 0.0, "payload": None})
    return {"cleared": True}


@app.get("/api/admin/cookies")
async def admin_cookies_get(request: Request):
    check_auth(request)
    ck = get_youtube_cookies()
    return {k: v for k, v in ck.items() if k != "content"}


@app.get("/api/health")
async def health():
    return {
        "status": "online",
        "service": "toolz-downloadz-api",
        "version": VERSION,
        "platforms": SUPPORTED,
        "auth": bool(API_SECRET_KEY),
        "cookies": {"youtube": get_youtube_cookies().get("verdict"), "instagram": bool(INSTAGRAM_COOKIES)},
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

    # Resolve + stream via explicit strategy chain. Each strategy is one
    # (resolve-mode, media-source) combo; first successful open wins.
    range_header = request.headers.get("range")
    errors: List[str] = []
    resp = None

    async def _resolve(fresh: bool) -> dict:
        key = _ckey(page_url, "False|None")
        result = None if fresh else cache_get(key)
        if not result or result.get("blocked"):
            loop = asyncio.get_running_loop()
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: extract_sync(page_url)),
                    timeout=max(EXTRACT_TIMEOUT, 5),
                )
            except asyncio.TimeoutError:
                raise HTTPException(504, f"Preparing the download timed out (~{EXTRACT_TIMEOUT}s). Tap Download once more — retries usually succeed.")
            except HTTPException:
                raise
            if result.get("blocked"):
                raise HTTPException(409, result.get("blocked_message", "Extraction blocked"))
            cache_set(key, result)
        return result

    def _pick(result):
        headers = dict(result.get("download_headers") or {})
        cookies = result.get("download_cookies")
        if f != "best" and f:
            for group in ("video", "audio"):
                for fmt in result.get("formats", {}).get(group, []):
                    if str(fmt.get("format_id")) == f:
                        return fmt["url"], dict(fmt.get("headers") or {}), fmt.get("cookies")
        media = result.get("download_url")
        if media and not headers:
            vids = result.get("formats", {}).get("video") or []
            if vids:
                headers = dict(vids[0].get("headers") or {})
        return media, headers, cookies

    def _sync_open(media_url, h):
        req = urllib.request.Request(media_url, headers=h)
        return urllib.request.urlopen(req, timeout=25)

    def _h_with_range(h):
        h2 = dict(h)
        if range_header:
            h2["Range"] = range_header
        return h2

    strategies = [("cached", False), ("fresh", True)]
    for label, fresh in strategies:
        try:
            result = await _resolve(fresh)
        except HTTPException as he:
            if he.status_code == 409 or label == "fresh":
                raise
            errors.append(str(he.detail)[:100])
            continue
        media, hdrs, cookies = _pick(result)
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.index:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=True)
