import time
import hashlib
import json
from typing import Optional, Any, Dict, Tuple

# Simple in-memory TTL cache + optional Redis backend if REDIS_URL is set.
# We use in-memory by default (works on Vercel/any stateless, but per-instance). Redis gives cross-instance cache.

_mem_cache: Dict[str, Tuple[float, Any]] = {}


def _key_for(url: str, opts_hash: str = "") -> str:
    raw = f"{url.strip()}|{opts_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> float:
    return time.time()


def mem_get(key: str) -> Optional[Any]:
    entry = _mem_cache.get(key)
    if not entry:
        return None
    exp, val = entry
    if _now() > exp:
        _mem_cache.pop(key, None)
        return None
    return val


def mem_set(key: str, value: Any, ttl: int) -> None:
    _mem_cache[key] = (_now() + ttl, value)
    # Simple eviction: if >1000 entries, drop oldest 20%
    if len(_mem_cache) > 1000:
        # sort by expiry
        sorted_keys = sorted(_mem_cache.items(), key=lambda x: x[1][0])
        for k, _ in sorted_keys[:200]:
            _mem_cache.pop(k, None)


# Optional Redis — lazy import
_redis_client = None
_redis_available = None


def _get_redis():
    global _redis_client, _redis_available
    if _redis_available is not None:
        return _redis_client
    try:
        from .config import get_settings
        settings = get_settings()
        if not settings.redis_url:
            _redis_available = False
            return None
        import redis.asyncio as aioredis  # type: ignore

        _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True, socket_timeout=2)
        _redis_available = True
        return _redis_client
    except Exception:
        _redis_available = False
        return None


async def cache_get(url: str, opts_hash: str = "") -> Optional[dict]:
    """
    Try Redis first, then memory. Returns deserialized dict or None.
    """
    key = _key_for(url, opts_hash)
    # Try redis async
    r = _get_redis()
    if r is not None:
        try:
            raw = await r.get(f"toolz:cache:{key}")
            if raw:
                return json.loads(raw)
        except Exception:
            pass  # fallback to mem
    # fallback mem (sync)
    v = mem_get(key)
    return v


async def cache_set(url: str, value: dict, ttl: int, opts_hash: str = "") -> None:
    key = _key_for(url, opts_hash)
    r = _get_redis()
    if r is not None:
        try:
            await r.setex(f"toolz:cache:{key}", ttl, json.dumps(value, default=str))
        except Exception:
            pass
    mem_set(key, value, ttl)


def cache_key(url: str) -> str:
    return _key_for(url)
