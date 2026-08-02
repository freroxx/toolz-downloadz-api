import os
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

app = FastAPI(
    title="toolz-downloadz-api",
    description="Serverless media extraction API for the Toolz ecosystem",
    version="1.0.0"
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
        "version": "1.0.0"
    }

@app.get("/api/extract", dependencies=[Depends(verify_api_key)])
async def extract_media(url: str):
    """
    Extracts media metadata and direct CDN URLs using yt-dlp.
    Optimized for Vercel Serverless (no disk writes).
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
        # Bypassing datacenter IP blocks with specific client emulation
        'extractor_args': {
            'youtube': {
                'player_client': ['tv', 'mweb', 'android']
            }
        },
        # User agent to mimic a real browser
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # extract_info with download=False ensures no files are saved to disk
            info = ydl.extract_info(url, download=False)

            # Filter and clean up formats for the response
            formats_list = []
            raw_formats = info.get('formats', [])

            for f in raw_formats:
                format_url = f.get('url')
                if not format_url:
                    continue

                formats_list.append({
                    'format_id': f.get('format_id'),
                    'ext': f.get('ext'),
                    'resolution': f.get('resolution') or f.get('format_note') or 'unknown',
                    'url': format_url,
                    'filesize': f.get('filesize') or f.get('filesize_approx'),
                    'vcodec': f.get('vcodec'),
                    'acodec': f.get('acodec')
                })

            # Robust fallback for download_url
            # Sometimes info['url'] is missing for high-quality DASH streams
            download_url = info.get('url')
            if not download_url and formats_list:
                # Fallback to the best available progressive/merged URL
                # Progressive formats usually have both vcodec and acodec != 'none'
                valid_formats = [f for f in formats_list if f['vcodec'] != 'none' and f['acodec'] != 'none']
                if valid_formats:
                    download_url = valid_formats[-1]['url']
                else:
                    download_url = formats_list[-1]['url']

            return {
                "title": info.get('title'),
                "thumbnail": info.get('thumbnail'),
                "duration": info.get('duration'),
                "uploader": info.get('uploader'),
                "download_url": download_url,
                "ext": info.get('ext'),
                "formats": formats_list
            }

    except Exception as e:
        # Catch and return yt-dlp errors gracefully
        raise HTTPException(
            status_code=400,
            detail=f"Extraction failed: {str(e)}"
        )

if __name__ == "__main__":
    import uvicorn
    # Local development entry point
    uvicorn.run(app, host="0.0.0.0", port=8000)
