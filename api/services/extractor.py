import os
import json
import urllib.request
import urllib.parse
import tempfile
from typing import Optional, Dict, Any, Tuple, List
import yt_dlp

from ..core.config import get_settings
from .platforms import detect_platform

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# yt-dlp tries these clients in order until one returns formats.
YOUTUBE_CLIENT_STRATEGIES = [
    ["android", "mweb", "web_safari"],
    ["ios", "web"],
    ["tv", "tv_downgraded"],
    ["android_vr"],
]


def _load_cookies_file() -> Optional[str]:
    """
    Prepare a Netscape cookies file for yt-dlp.
    Prefers path in YT_COOKIES_FILE, otherwise writes YOUTUBE_COOKIES env content to temp file.
    """
    settings = get_settings()
    configured = settings.yt_cookies_file
    if configured and os.path.exists(configured):
        return configured

    content = settings.youtube_cookies
    if not content or not content.strip():
        return None

    cookie_path = os.path.join("/tmp", "yt_cookies.txt")
    try:
        with open(cookie_path, "w", encoding="utf-8") as f:
            f.write(content if content.endswith("\n") else content + "\n")
        return cookie_path
    except Exception:
        return None


def build_ydl_opts(
    platform: str,
    youtube_clients: Optional[List[str]] = None,
    custom_format: Optional[str] = None,
    audio_only: bool = False,
    playlist: bool = False,
    playlist_items: Optional[str] = None,
    include_subtitles: bool = False,
    extra_extractor_args: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    settings = get_settings()
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "nocheckcertificate": True,
        "ignoreerrors": False,
        "logtostderr": False,
        "socket_timeout": settings.effective_socket_timeout,
        "geo_bypass": True,
        "check_formats": False,
        "http_headers": BASE_HEADERS,
        "extractor_args": {},
        # Playlist handling
        "noplaylist": not playlist,
        "extract_flat": False,
    }

    if playlist_items:
        # yt-dlp expects --playlist-items handling via playlist_items / playliststart etc
        # we use "playlist_items" extractor arg via opts
        opts["playlist_items"] = playlist_items

    cookie_file = _load_cookies_file()
    if cookie_file:
        opts["cookiefile"] = cookie_file

    pot_url = settings.resolved_pot_url
    if pot_url:
        opts["extractor_args"]["youtubepot-bgutilhttp"] = {"base_url": pot_url}

    if custom_format:
        opts["format"] = custom_format
    elif audio_only:
        opts["format"] = "bestaudio/best"

    if include_subtitles:
        opts["writesubtitles"] = False  # we just want metadata, not files
        opts["listsubtitles"] = False
        # keep subtitles in info dict
        opts["getcomments"] = False

    # Merge extra extractor args (for power users)
    if extra_extractor_args:
        for k, v in extra_extractor_args.items():
            opts["extractor_args"][k] = v

    if platform == "youtube":
        if not custom_format and not audio_only:
            opts["format"] = "best[ext=mp4]/best"
        opts["youtube_include_dash_manifest"] = False
        opts["youtube_include_hls_manifest"] = False
        opts["extractor_args"].setdefault("youtube", {})
        if not pot_url:
            opts["extractor_args"]["youtube"].setdefault(
                "player_client", youtube_clients or ["android", "mweb", "web_safari"]
            )
        opts["extractor_args"]["youtube"].setdefault("player_skip", ["webpage", "configs"])

    elif platform == "instagram":
        opts["extractor_args"]["instagram"] = {"get_comments": False}
        # Merge default IG args if user provided custom
        if extra_extractor_args and "instagram" in extra_extractor_args:
            opts["extractor_args"]["instagram"].update(extra_extractor_args["instagram"])

    elif platform == "tiktok":
        opts["extractor_args"]["tiktok"] = {
            "api_hostname": "api16-normal-useast5.us.tiktok.com",
            "web_instance_url": "https://www.tiktok.com/",
        }
        if extra_extractor_args and "tiktok" in extra_extractor_args:
            opts["extractor_args"]["tiktok"].update(extra_extractor_args["tiktok"])

    elif platform == "twitter":
        opts["extractor_args"]["twitter"] = {"mobile_redirect": True}

    elif platform == "facebook":
        # Facebook often needs no special args; keep generic
        pass

    elif platform == "soundcloud":
        # SoundCloud playlist support
        pass

    return opts


def youtube_oembed(url: str) -> Optional[dict]:
    """Fetch basic metadata from YouTube's public oEmbed endpoint. Never blocked."""
    try:
        oembed_url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe="")
        req = urllib.request.Request(oembed_url, headers=BASE_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {
            "title": data.get("title"),
            "uploader": data.get("author_name"),
            "uploader_url": data.get("author_url"),
            "thumbnail": data.get("thumbnail_url"),
        }
    except Exception:
        return None


def normalize_streams(info: dict) -> Tuple[List[dict], List[dict]]:
    video_streams: List[dict] = []
    audio_streams: List[dict] = []

    for f in info.get("formats", []) or []:
        format_url = f.get("url")
        if not format_url:
            continue
        fmt = {
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "resolution": f.get("resolution") or f.get("format_note") or "unknown",
            "url": format_url,
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
            "fps": f.get("fps"),
            "tbr": f.get("tbr"),
            "width": f.get("width"),
            "height": f.get("height"),
            "asr": f.get("asr"),
            "abr": f.get("abr"),
            "vbr": f.get("vbr"),
            "headers": dict(f.get("http_headers") or {}),
        }
        if f.get("vcodec") == "none":
            audio_streams.append(fmt)
        else:
            video_streams.append(fmt)

    # Sort by quality: prefer higher resolution / bitrate, then filesize
    def _video_score(x):
        # height if available, else filesize
        h = x.get("height") or 0
        if isinstance(h, str):
            try:
                h = int(h.split("x")[-1])
            except:
                h = 0
        return (h, x.get("tbr") or 0, x.get("filesize") or 0)

    def _audio_score(x):
        return (x.get("abr") or x.get("tbr") or 0, x.get("filesize") or 0)

    video_streams.sort(key=_video_score, reverse=True)
    audio_streams.sort(key=_audio_score, reverse=True)
    return video_streams, audio_streams


def _extract_youtube(strategies: List[List[str]], url: str, ydl_opts: Dict[str, Any]) -> dict:
    last_error = "YouTube extraction failed"
    settings = get_settings()
    # On Vercel, try fewer strategies to stay within function timeout (maxDuration 10-30s)
    if settings.is_vercel:
        strategies = strategies[:2]  # android+mweb first, then ios — covers 95% of cases
    base_youtube_args = dict(ydl_opts["extractor_args"].get("youtube") or {})
    base_youtube_args.pop("player_client", None)
    for clients in strategies:
        opts = dict(ydl_opts)
        opts["extractor_args"] = dict(ydl_opts["extractor_args"])
        youtube_args = dict(base_youtube_args)
        youtube_args["player_client"] = clients
        opts["extractor_args"]["youtube"] = youtube_args
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            last_error = str(e)
            # On Vercel, don't waste time cycling all clients if it's clearly a bot block
            if settings.is_vercel and ("Sign in to confirm" in str(e) or "not a bot" in str(e)):
                break
            continue
    raise RuntimeError(last_error)


def extract_youtube_pytubefix(url: str) -> Optional[dict]:
    """Fallback engine for YouTube using pytubefix (different extractor stack)."""
    try:
        from pytubefix import YouTube
    except Exception:
        return None

    try:
        yt = YouTube(url)
        video_streams = []
        audio_streams = []
        for s in yt.streams:
            if not s.url:
                continue
            fmt = {
                "format_id": s.itag,
                "ext": s.mime_type.split("/")[-1] if s.mime_type else "mp4",
                "resolution": s.resolution or s.abr or "audio",
                "url": s.url,
                "filesize": s.filesize,
                "vcodec": "none" if not s.resolution else "avc1.64001f",
                "acodec": "mp4a.40.2" if s.abr else None,
            }
            if s.abr or not s.resolution:
                audio_streams.append(fmt)
            else:
                video_streams.append(fmt)

        download_url = None
        for s in yt.streams.filter(progressive=True, file_extension="mp4"):
            download_url = s.url
            break

        return {
            "platform": "youtube",
            "title": yt.title,
            "thumbnail": yt.thumbnail_url,
            "duration": getattr(yt, "length", None),
            "uploader": yt.author,
            "uploader_url": None,
            "stats": {"views": None, "likes": None, "comments": None, "reposts": None, "creator_followers": None},
            "upload_date": None,
            "download_url": download_url,
            "download_headers": {"User-Agent": "com.google.android.youtube/19.09.37 WebKit", "os_version": "Android 13"},
            "ext": "mp4",
            "blocked": False,
            "formats": {"video": video_streams, "audio": audio_streams},
        }
    except Exception:
        return None


def clean_error(message: str, platform: str) -> str:
    msg = message.strip()
    if msg.startswith("ERROR:"):
        msg = msg[len("ERROR:") :].strip()
    if not msg:
        msg = "Extraction failed for unknown reasons."
    hints = {
        "youtube": "YouTube blocked this Vercel IP. We returned oEmbed metadata; add YOUTUBE_COOKIES in Vercel env to bypass, or retry in 2-3 min.",
        "instagram": "Instagram often requires login. Add YOUTUBE_COOKIES/IG cookies in Vercel env or try a public post.",
        "tiktok": "TikTok blocks some regions/devices on Vercel; try a different video or web URL.",
        "twitter": "X/Twitter often requires login. Try a public post.",
        "reddit": "Reddit sometimes requires login; try another post.",
        "facebook": "Facebook may require login or block Vercel IPs; try public video or add cookies.",
        "soundcloud": "SoundCloud track may be private or blocked in this region.",
        "twitch": "Twitch VOD/clip may require auth or be deleted.",
        "generic": "The platform rejected the request or URL not supported. Try direct media URL.",
    }
    return f"{msg} ({hints.get(platform, hints['generic'])})"


def build_response(platform: str, info: dict, original_url: Optional[str] = None) -> dict:
    # Handle playlist case: info has 'entries'
    if "entries" in info:
        entries = list(info.get("entries") or [])
        # Don't fully expand playlist entries here — just preview
        # For API v2, we return playlist metadata + first 10 entries preview
        preview = []
        for e in entries[:10]:
            if isinstance(e, dict):
                preview.append(
                    {
                        "title": e.get("title"),
                        "url": e.get("webpage_url") or e.get("url"),
                        "thumbnail": e.get("thumbnail"),
                        "duration": e.get("duration"),
                        "uploader": e.get("uploader"),
                    }
                )
        # playlist self metadata
        video_streams, audio_streams = normalize_streams(info)  # may be empty for playlist
        return {
            "platform": platform,
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": None,
            "uploader": info.get("uploader"),
            "uploader_url": info.get("uploader_url"),
            "stats": {
                "views": info.get("view_count"),
                "likes": info.get("like_count"),
                "comments": info.get("comment_count"),
                "reposts": info.get("repost_count") or info.get("retweet_count"),
                "creator_followers": info.get("channel_follower_count") or info.get("uploader_follower_count"),
            },
            "upload_date": info.get("upload_date"),
            "description": info.get("description"),
            "tags": info.get("tags"),
            "categories": info.get("categories"),
            "subtitles": info.get("subtitles"),
            "chapters": info.get("chapters"),
            "is_live": info.get("is_live"),
            "was_live": info.get("was_live"),
            "playlist_count": info.get("playlist_count") or len(entries),
            "playlist_entries": preview,
            "entries_total": len(entries),
            "download_url": None,
            "download_headers": {},
            "ext": info.get("ext"),
            "blocked": False,
            "formats": {"video": video_streams, "audio": audio_streams},
            "extractor": info.get("extractor"),
            "webpage_url": info.get("webpage_url"),
            "original_url": original_url,
        }

    video_streams, audio_streams = normalize_streams(info)

    download_url = info.get("url")
    headers_for_best = dict(info.get("http_headers") or {})
    if not download_url:
        merged = [f for f in video_streams if f["vcodec"] not in (None, "none") and f["acodec"] not in (None, "none")]
        if merged:
            download_url = merged[0]["url"]
            headers_for_best = dict(merged[0].get("headers") or {})
        elif video_streams:
            download_url = video_streams[0]["url"]
            headers_for_best = dict(video_streams[0].get("headers") or {})
        elif audio_streams:
            download_url = audio_streams[0]["url"]
            headers_for_best = dict(audio_streams[0].get("headers") or {})

    stats = {
        "views": info.get("view_count"),
        "likes": info.get("like_count"),
        "comments": info.get("comment_count"),
        "reposts": info.get("repost_count") or info.get("retweet_count"),
        "creator_followers": info.get("channel_follower_count") or info.get("uploader_follower_count"),
    }

    return {
        "platform": platform,
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "uploader_url": info.get("uploader_url"),
        "stats": stats,
        "upload_date": info.get("upload_date"),
        "description": info.get("description"),
        "tags": info.get("tags"),
        "categories": info.get("categories"),
        "subtitles": info.get("subtitles"),
        "chapters": info.get("chapters"),
        "is_live": info.get("is_live"),
        "was_live": info.get("was_live"),
        "like_count": info.get("like_count"),
        "view_count": info.get("view_count"),
        "comment_count": info.get("comment_count"),
        "download_url": download_url,
        "download_headers": headers_for_best,
        "ext": info.get("ext"),
        "blocked": False,
        "formats": {"video": video_streams, "audio": audio_streams},
        "extractor": info.get("extractor"),
        "extractor_key": info.get("extractor_key"),
        "webpage_url": info.get("webpage_url"),
        "original_url": original_url,
    }


def blocked_response(meta: dict, original_url: Optional[str] = None) -> dict:
    return {
        "platform": "youtube",
        "title": meta["title"],
        "thumbnail": meta["thumbnail"],
        "duration": None,
        "uploader": meta["uploader"],
        "uploader_url": meta["uploader_url"],
        "stats": {"views": None, "likes": None, "comments": None, "reposts": None, "creator_followers": None},
        "description": None,
        "download_url": None,
        "upload_date": None,
        "download_headers": {},
        "blocked": True,
        "blocked_message": (
            "YouTube is blocking this Vercel datacenter IP right now (common on serverless). "
            "The title/thumbnail below are from YouTube's public oEmbed API (never blocked). "
            "Fix: add YOUTUBE_COOKIES in Vercel env (export from your browser) or set YT_DLP_POT_PROVIDER_URL to an external POT server. "
            "Otherwise retry in a few minutes — blocks are often temporary."
        ),
        "formats": {"video": [], "audio": []},
        "original_url": original_url,
    }


# Main orchestration — called by routers
def extract_media_sync(
    url: str,
    custom_format: Optional[str] = None,
    audio_only: bool = False,
    playlist: bool = False,
    playlist_items: Optional[str] = None,
    include_subtitles: bool = False,
    extra_extractor_args: Optional[Dict[str, Any]] = None,
) -> dict:
    platform = detect_platform(url)

    if platform == "youtube" and not playlist:
        # Engine 1: yt-dlp with multi-client rotation
        try:
            info = _extract_youtube(
                YOUTUBE_CLIENT_STRATEGIES,
                url,
                build_ydl_opts(
                    "youtube",
                    custom_format=custom_format,
                    audio_only=audio_only,
                    playlist=playlist,
                    playlist_items=playlist_items,
                    include_subtitles=include_subtitles,
                    extra_extractor_args=extra_extractor_args,
                ),
            )
            return build_response(platform, info, original_url=url)
        except Exception:
            pass

        # Engine 2: pytubefix
        try:
            info = extract_youtube_pytubefix(url)
            if info:
                info["original_url"] = url
                return info
        except Exception:
            pass

        # Engine 3: oEmbed fallback
        meta = youtube_oembed(url)
        if meta:
            return blocked_response(meta, original_url=url)
        raise RuntimeError(
            "Extraction failed: YouTube rejected the request from this Vercel IP. "
            "Add YOUTUBE_COOKIES in Vercel env (see README) to bypass, or retry — blocks are temporary."
        )

    # Generic path for all other platforms (and YouTube playlists)
    ydl_opts = build_ydl_opts(
        platform,
        custom_format=custom_format,
        audio_only=audio_only,
        playlist=playlist,
        playlist_items=playlist_items,
        include_subtitles=include_subtitles,
        extra_extractor_args=extra_extractor_args,
    )
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return build_response(platform, info, original_url=url)
    except Exception as e:
        raise RuntimeError(clean_error(str(e), platform)) from e
