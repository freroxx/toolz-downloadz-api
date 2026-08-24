from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field, HttpUrl


class HealthResponse(BaseModel):
    status: str = "online"
    service: str = "toolz-downloadz-api"
    version: str
    environment: str
    uptime_seconds: Optional[int] = None
    cache: str = "memory"


class PlatformInfo(BaseModel):
    platform: str
    title: Optional[str] = None
    thumbnail: Optional[str] = None
    duration: Optional[int] = None
    uploader: Optional[str] = None
    uploader_url: Optional[str] = None
    upload_date: Optional[str] = None
    stats: Dict[str, Optional[int]] = Field(default_factory=dict)
    blocked: bool = False
    blocked_message: Optional[str] = None
    ext: Optional[str] = None
    download_url: Optional[str] = None
    download_headers: Dict[str, str] = Field(default_factory=dict)
    formats: Dict[str, List[Dict[str, Any]]] = Field(default_factory=lambda: {"video": [], "audio": []})
    # Extended v2 fields
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    categories: Optional[List[str]] = None
    subtitles: Optional[Dict[str, List[Dict[str, Any]]]] = None
    chapters: Optional[List[Dict[str, Any]]] = None
    is_live: Optional[bool] = None
    was_live: Optional[bool] = None
    like_count: Optional[int] = None
    view_count: Optional[int] = None
    comment_count: Optional[int] = None
    # For playlist
    playlist_count: Optional[int] = None
    playlist_entries: Optional[List[Dict[str, Any]]] = None
    extractor: Optional[str] = None
    webpage_url: Optional[str] = None
    original_url: Optional[str] = None


class ExtractRequest(BaseModel):
    url: str = Field(..., description="Media URL to extract", min_length=10, max_length=2048)
    # Customization options
    format: Optional[str] = Field(None, description="yt-dlp format selector, e.g. 'bestvideo+bestaudio/best'")
    audio_only: bool = Field(False, description="Prefer audio-only formats")
    playlist: bool = Field(False, description="If URL is playlist, return entries")
    playlist_items: Optional[str] = Field(None, description="e.g. '1-10' to limit playlist")
    subtitles: bool = Field(False, description="Include subtitles metadata")
    cookies: Optional[str] = Field(None, description="Optional Netscape cookies content override")


class FormatOption(BaseModel):
    format_id: Optional[str]
    ext: Optional[str]
    resolution: Optional[str]
    url: str
    filesize: Optional[int]
    vcodec: Optional[str]
    acodec: Optional[str]
    fps: Optional[int] = None
    tbr: Optional[float] = None
    headers: Dict[str, str] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    detail: str
    hint: Optional[str] = None
    platform: Optional[str] = None
    request_id: Optional[str] = None


class PlatformsResponse(BaseModel):
    platforms: List[Dict[str, Any]]
