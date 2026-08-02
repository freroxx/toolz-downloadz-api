import os
import json
import urllib.request
import urllib.parse
from typing import Optional, Dict, Any, Tuple, List
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

app = FastAPI(
    title="toolz-downloadz-api",
    description="Serverless media extraction API for the Toolz ecosystem",
    version="1.2.0"
)

# Enable CORS for all domains to support Android and Web clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_api_secret():
    return os.getenv("API_SECRET_KEY")


def detect_platform(url: str) -> str:
    """Analyzes the URL domain and returns a clean platform indicator."""
    url = url.lower()
    if any(domain in url for domain in ["youtube.com", "youtu.be"]):
        return "youtube"
    elif "tiktok.com" in url:
        return "tiktok"
    elif "instagram.com" in url:
        return "instagram"
    elif any(domain in url for domain in ["twitter.com", "x.com"]):
        return "twitter"
    elif "reddit.com" in url:
        return "reddit"
    return "generic"


async def verify_api_key(x_api_key: Optional[str] = Header(None)):
    secret = get_api_secret()
    if not secret:
        raise HTTPException(
            status_code=500,
            detail="Server configuration error: API_SECRET_KEY is not set in Vercel Environment Variables."
        )
    if x_api_key != secret:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Invalid or missing X-API-KEY header"
        )


def _load_cookies_file() -> Optional[str]:
    """
    Prepare a Netscape cookies file for yt-dlp.
    Prefers a path in YT_COOKIES_FILE, otherwise writes the YOUTUBE_COOKIES
    env content to a temp file. Returns None when nothing is configured.
    """
    configured = os.getenv("YT_COOKIES_FILE")
    if configured and os.path.exists(configured):
        return configured

    content = os.getenv("YOUTUBE_COOKIES")
    if not content or not content.strip():
        return None

    cookie_path = os.path.join('/tmp', 'yt_cookies.txt')
    try:
        with open(cookie_path, 'w', encoding='utf-8') as f:
            f.write(content if content.endswith('\n') else content + '\n')
        return cookie_path
    except Exception:
        return None


BASE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}

# yt-dlp tries these clients in order until one returns formats.
YOUTUBE_CLIENT_STRATEGIES = [
    ['android', 'mweb', 'web_safari'],
    ['ios', 'web'],
    ['tv', 'tv_downgraded'],
    ['android_vr'],
]


def build_ydl_opts(platform: str, youtube_clients: Optional[List[str]] = None) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'socket_timeout': 10,
        'geo_bypass': True,
        'check_formats': False,
        'http_headers': BASE_HEADERS,
        'extractor_args': {},
    }

    cookie_file = _load_cookies_file()
    if cookie_file:
        opts['cookiefile'] = cookie_file

    if platform == 'youtube':
        opts['format'] = 'best[ext=mp4]/best'
        opts['youtube_include_dash_manifest'] = False
        opts['youtube_include_hls_manifest'] = False
        opts['extractor_args']['youtube'] = {
            'player_client': youtube_clients or ['android', 'mweb', 'web_safari'],
            'player_skip': ['webpage', 'configs'],
        }

    elif platform == 'instagram':
        opts['extractor_args']['instagram'] = {'get_comments': False}

    elif platform == 'tiktok':
        opts['extractor_args']['tiktok'] = {'api_hostname': 'api22-normal-c-useast2a.tiktok.com'}

    elif platform == 'twitter':
        opts['extractor_args']['twitter'] = {'mobile_redirect': True}

    return opts


def youtube_oembed(url: str) -> Optional[dict]:
    """Fetch basic metadata from YouTube's public oEmbed endpoint. Never blocked."""
    try:
        oembed_url = 'https://www.youtube.com/oembed?format=json&url=' + \
            urllib.parse.quote(url, safe='')
        req = urllib.request.Request(oembed_url, headers=BASE_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return {
            'title': data.get('title'),
            'uploader': data.get('author_name'),
            'uploader_url': data.get('author_url'),
            'thumbnail': data.get('thumbnail_url'),
        }
    except Exception:
        return None


def normalize_streams(info: dict) -> Tuple[List[dict], List[dict]]:
    video_streams: List[dict] = []
    audio_streams: List[dict] = []

    for f in info.get('formats', []):
        format_url = f.get('url')
        if not format_url:
            continue
        fmt = {
            'format_id': f.get('format_id'),
            'ext': f.get('ext'),
            'resolution': f.get('resolution') or f.get('format_note') or 'unknown',
            'url': format_url,
            'filesize': f.get('filesize') or f.get('filesize_approx'),
            'vcodec': f.get('vcodec'),
            'acodec': f.get('acodec'),
        }
        if f.get('vcodec') == 'none':
            audio_streams.append(fmt)
        else:
            video_streams.append(fmt)

    video_streams.sort(key=lambda x: x.get('filesize') or 0, reverse=True)
    audio_streams.sort(key=lambda x: x.get('filesize') or 0, reverse=True)
    return video_streams, audio_streams


def _extract_youtube(strategies: List[List[str]], url: str, ydl_opts: Dict[str, Any]) -> dict:
    last_error = "YouTube extraction failed"
    for clients in strategies:
        opts = dict(ydl_opts)
        opts['extractor_args'] = dict(ydl_opts['extractor_args'])
        opts['extractor_args']['youtube'] = {
            'player_client': clients,
            'player_skip': ['webpage', 'configs'],
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            last_error = str(e)
            continue
    raise RuntimeError(last_error)


@app.get("/api/health")
async def health_check():
    return {"status": "online", "service": "toolz-downloadz-api", "version": "1.2.0"}


@app.get("/api/extract", dependencies=[Depends(verify_api_key)])
async def extract_media(url: str):
    """Extract media metadata and direct CDN URLs using yt-dlp with fallbacks."""
    if not url.startswith(('http://', 'https://')):
        raise HTTPException(status_code=400, detail="Invalid URL. Must start with http:// or https://")

    platform = detect_platform(url)

    if platform == 'youtube':
        # Try yt-dlp with client rotation; on repeat blocks pivot to oEmbed metadata.
        try:
            info = _extract_youtube(YOUTUBE_CLIENT_STRATEGIES, url, build_ydl_opts('youtube'))
            return build_response(platform, info)
        except Exception:
            meta = youtube_oembed(url)
            if meta:
                return blocked_response(meta)
            raise HTTPException(
                status_code=400,
                detail=("Extraction failed: YouTube rejected the request from this server. "
                        "This is usually a temporary IP reputation block. Reinstall/update "
                        "the API cookie env or try again shortly.")
            )

    try:
        ydl_opts = build_ydl_opts(platform)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return build_response(platform, info)
    except Exception as e:
        raise HTTPException(status_code=400, detail=clean_error(str(e), platform))


def clean_error(message: str, platform: str) -> str:
    """Strip yt-dlp formatting noise for the client, add friendly guidance."""
    msg = message.strip()
    if msg.startswith('ERROR:'):
        msg = msg[len('ERROR:'):].strip()
    if not msg:
        msg = 'Extraction failed for unknown reasons.'
    hints = {
        'youtube': 'This may be a temporary YouTube block on this network; try again shortly, '
                   'or a different playlist/URL.',
        'instagram': 'Instagram often requires login. Try a public post or an alternate URL.',
        'tiktok': 'TikTok blocks some regions/devices from this network; try a different video.',
        'twitter': 'X/Twitter often requires login. Try a public post from another account.',
        'reddit': 'Reddit sometimes requires login or blocks third-party clients; try another post.',
        'generic': 'The platform rejected the request or the URL is not supported.',
    }
    return f"{msg} ({hints.get(platform, hints['generic'])})"


def build_response(platform: str, info: dict) -> dict:
    video_streams, audio_streams = normalize_streams(info)

    download_url = info.get('url')
    if not download_url:
        merged = [f for f in video_streams
                  if f['vcodec'] not in (None, 'none') and f['acodec'] not in (None, 'none')]
        if merged:
            download_url = merged[0]['url']
        elif video_streams:
            download_url = video_streams[0]['url']
        elif audio_streams:
            download_url = audio_streams[0]['url']

    stats = {
        "views": info.get('view_count'),
        "likes": info.get('like_count'),
        "comments": info.get('comment_count'),
        "reposts": info.get('repost_count') or info.get('retweet_count'),
        "creator_followers": info.get('channel_follower_count') or info.get('uploader_follower_count'),
    }

    return {
        "platform": platform,
        "title": info.get('title'),
        "thumbnail": info.get('thumbnail'),
        "duration": info.get('duration'),
        "uploader": info.get('uploader'),
        "uploader_url": info.get('uploader_url'),
        "stats": stats,
        "upload_date": info.get('upload_date'),
        "download_url": download_url,
        "ext": info.get('ext'),
        "blocked": False,
        "formats": {"video": video_streams, "audio": audio_streams},
    }


def blocked_response(meta: dict) -> dict:
    return {
        "platform": "youtube",
        "title": meta['title'],
        "thumbnail": meta['thumbnail'],
        "duration": None,
        "uploader": meta['uploader'],
        "uploader_url": meta['uploader_url'],
        "stats": {"views": None, "likes": None, "comments": None,
                  "reposts": None, "creator_followers": None},
        "download_url": None,
        "upload_date": None,
        "blocked": True,
        "blocked_message": (
            "YouTube is blocking automated access from this server's IP region right now. "
            "The video info below comes from YouTube's public API; direct streaming links "
            "will appear once the block lifts. Try again in a few minutes."
        ),
        "formats": {"video": [], "audio": []},
    }