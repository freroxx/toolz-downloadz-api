import os
import re
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

app = FastAPI(
    title="toolz-downloadz-api",
    description="Serverless media extraction API for the Toolz ecosystem",
    version="1.1.0"
)

# Enable CORS for all domains to support Android and Web clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration from environment variables
API_SECRET_KEY = os.getenv("API_SECRET_KEY")

def detect_platform(url: str) -> str:
    """
    Analyzes the URL domain and returns a clean string indicator:
    'youtube', 'tiktok', 'instagram', 'twitter', 'reddit', or 'generic'.
    """
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
    """
    Dependency to verify the X-API-KEY header.
    Returns 401 Unauthorized if the key is incorrect.
    Returns 500 Internal Server Error if the key is not configured on the server.
    """
    if not API_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="Server configuration error: API_SECRET_KEY is not set."
        )

    if x_api_key != API_SECRET_KEY:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Invalid or missing X-API-KEY header"
        )

@app.get("/")
async def health_check():
    """Simple health check endpoint."""
    return {
        "status": "online",
        "service": "toolz-downloadz-api",
        "version": "1.1.0"
    }

@app.get("/api/extract", dependencies=[Depends(verify_api_key)])
async def extract_media(url: str):
    """
    Extracts media metadata and direct CDN URLs using yt-dlp.
    Optimized for Vercel Serverless (no disk writes, socket timeout).
    """
    # yt-dlp configuration optimized for serverless and bot-bypass
    ydl_opts = {
        # Prefer mp4 progressive streams for direct extraction without FFmpeg
        'format': 'best[ext=mp4]/best',
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'skip_download': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'socket_timeout': 7,
        'geo_bypass': True,
        'check_formats': False,
        # Bypassing datacenter IP blocks with specific client emulation
        'extractor_args': {
            'youtube': {
                'player_client': ['tv', 'mweb', 'android'],
                'player_skip': ['webpage', 'configs']
            },
            'instagram': {
                'get_comments': False
            }
        },
        # User agent to mimic a real browser
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    }

    try:
        platform = detect_platform(url)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # extract_info with download=False ensures no files are saved to disk
            info = ydl.extract_info(url, download=False)

            # Categorize and clean up formats for the response
            video_streams = []
            audio_streams = []
            raw_formats = info.get('formats', [])

            for f in raw_formats:
                format_url = f.get('url')
                if not format_url:
                    continue

                fmt_data = {
                    'format_id': f.get('format_id'),
                    'ext': f.get('ext'),
                    'resolution': f.get('resolution') or f.get('format_note') or 'unknown',
                    'url': format_url,
                    'filesize': f.get('filesize') or f.get('filesize_approx'),
                    'vcodec': f.get('vcodec'),
                    'acodec': f.get('acodec')
                }

                if f.get('vcodec') == 'none':
                    audio_streams.append(fmt_data)
                else:
                    video_streams.append(fmt_data)

            # Sort streams by filesize descending as a heuristic for quality
            video_streams.sort(key=lambda x: (x.get('filesize') or 0), reverse=True)
            audio_streams.sort(key=lambda x: (x.get('filesize') or 0), reverse=True)

            # Extract stats with safe fallbacks
            stats = {
                "views": info.get('view_count'),
                "likes": info.get('like_count'),
                "comments": info.get('comment_count'),
                "reposts": info.get('repost_count') or info.get('retweet_count'),
                "creator_followers": info.get('channel_follower_count') or info.get('uploader_follower_count')
            }

            # Robust fallback for download_url
            download_url = info.get('url')
            if not download_url:
                # Fallback to the best available progressive/merged URL from video streams
                valid_video = [f for f in video_streams if f['vcodec'] != 'none' and f['acodec'] != 'none']
                if valid_video:
                    download_url = valid_video[0]['url']
                elif video_streams:
                    download_url = video_streams[0]['url']
                elif audio_streams:
                    download_url = audio_streams[0]['url']

            return {
                "platform": platform,
                "title": info.get('title'),
                "thumbnail": info.get('thumbnail'),
                "duration": info.get('duration'),
                "uploader": info.get('uploader'),
                "uploader_url": info.get('uploader_url'),
                "stats": stats,
                "download_url": download_url,
                "ext": info.get('ext'),
                "formats": {
                    "video": video_streams,
                    "audio": audio_streams
                }
            }

    except Exception as e:
        # Catch and return yt-dlp errors gracefully
        # Strip potential HTML from error messages to avoid client parsing issues
        error_msg = str(e)
        if "<!DOCTYPE" in error_msg or "<html" in error_msg.lower():
            error_msg = "Platform blocked the request or returned an invalid response. Try again later."

        raise HTTPException(
            status_code=400,
            detail=f"Extraction failed: {error_msg}"
        )

if __name__ == "__main__":
    import uvicorn
    # Local development entry point
    uvicorn.run(app, host="0.0.0.0", port=8000)
