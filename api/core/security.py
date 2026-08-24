import ipaddress
import urllib.parse
from typing import Optional
from fastapi import Header, HTTPException, Request, Depends
from .config import get_settings

# Blocklist for SSRF / private networks
_BLOCKED_HOSTS = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
}

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local + AWS metadata
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_private_host(host: str) -> bool:
    h = host.lower().strip()
    if h in _BLOCKED_HOSTS:
        return True
    if h.endswith(".internal") or h.endswith(".local"):
        return True
    # Try IP parse
    try:
        ip = ipaddress.ip_address(h)
        return any(ip in net for net in _PRIVATE_NETWORKS) or ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


def validate_url(url: str, max_length: int = 2048) -> str:
    """
    Strict URL validation. Raises HTTPException 400 if invalid.
    Allows any http/https URL but blocks private / internal hosts.
    This is intentional: yt-dlp supports 1800+ sites, so we don't restrict to a static allowlist for extraction.
    The download proxy still enforces a media CDN allowlist.
    """
    if not url or not isinstance(url, str):
        raise HTTPException(status_code=400, detail="Missing url parameter")
    url = url.strip()
    if len(url) > max_length:
        raise HTTPException(status_code=400, detail=f"URL too long (max {max_length} chars)")
    if len(url) < 10:
        raise HTTPException(status_code=400, detail="URL too short")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Invalid URL. Must start with http:// or https://")

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL format")

    if not parsed.netloc or "." not in parsed.netloc:
        raise HTTPException(status_code=400, detail="Invalid URL: missing domain")

    host = parsed.hostname or ""
    if _is_private_host(host):
        raise HTTPException(status_code=400, detail="URL host not allowed (private/internal)")

    # Block common SSRF bypass tricks
    if "@" in parsed.netloc:
        # user:pass@host — often used to hide real host
        raise HTTPException(status_code=400, detail="URL with credentials not allowed")

    return url


async def verify_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-KEY"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> None:
    """
    Verifies API key from X-API-KEY or Authorization: Bearer <key>.
    If no server secret is configured, allow all (with warning) — but in production, require it.
    """
    settings = get_settings()
    allowed = settings.allowed_api_keys

    # Extract key from various headers
    provided: Optional[str] = None
    if x_api_key:
        provided = x_api_key.strip()
    elif authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    else:
        # Also support query param for testing convenience? No — header only for prod.
        # But allow ?api_key for backwards compat if needed
        qp = request.query_params.get("api_key") or request.query_params.get("key")
        if qp:
            provided = qp.strip()

    if not allowed:
        # No secret configured — this is a misconfiguration, but don't lock users out during dev
        # In production we error loudly
        if settings.environment == "production":
            raise HTTPException(
                status_code=500,
                detail="Server configuration error: API_SECRET_KEY is not set. Set it in environment variables.",
            )
        # In dev, allow without key but log warning
        return

    if not provided:
        raise HTTPException(status_code=401, detail="Unauthorized: Missing X-API-KEY header (or Authorization: Bearer)")

    if provided not in allowed:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid API key")
