import time
import hashlib
import json
import urllib.request
import urllib.error
import urllib.parse
from typing import Optional, Any, Dict, Tuple
import os

# Vercel-optimized cache:
# - In-memory TTL (always available, per-lambda)
# - Optional Upstash Redis REST (Vercel KV) via HTTP — survives across lambdas
# - Optional classic Redis via REDIS_URL (if you connect Upstash via redis://)
# On Vercel hobby, lambdas are short-lived, so Upstash gives real persistence.

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
    if len(_mem_cache) > 1000:
        sorted_keys = sorted(_mem_cache.items(), key=lambda x: x[1][0])
        for k, _ in sorted_keys[:200]:
            _mem_cache.pop(k, None)


# Redis classic (TCP) — lazy
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
        # Don't try redis on Vercel if URL is not present? It can work but prefer Upstash REST.
        import redis.asyncio as aioredis  # type: ignore
        _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True, socket_timeout=2)
        _redis_available = True
        return _redis_client
    except Exception:
        _redis_available = False
        return None


def _upstash_get(key: str) -> Optional[Any]:
    """Sync Upstash REST GET via urllib (works on Vercel without extra deps)"""
    try:
        from .config import get_settings
        s = get_settings()
        url = s.resolved_upstash_url
        token = s.resolved_upstash_token
        if not url or not token:
            return None
        # Upstash REST: POST {url}/get/{key} with Authorization: Bearer {token}
        # Vercel KV uses same spec.
        endpoint = f"{url.rstrip('/')}/get/{urllib.parse.quote(key, safe='')}"
        req = urllib.request.Request(endpoint, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            # Upstash returns {"result": "json-string"} or {"result": null}
            raw = data.get("result")
            if raw:
                return json.loads(raw) if isinstance(raw, str) else raw
            return None
    except Exception:
        return None


def _upstash_set(key: str, value: Any, ttl: int) -> bool:
    try:
        from .config import get_settings
        s = get_settings()
        url = s.resolved_upstash_url
        token = s.resolved_upstash_token
        if not url or not token:
            return False
        # Upstash SET with EX: POST {url}/set/{key}/{value}?EX={ttl}
        # Value must be URL-encoded JSON
        val_str = json.dumps(value, default=str)
        # Use pipeline: set key value EX ttl
        # Upstash REST supports: /set/{key}/{value}?EX={ttl}
        endpoint = f"{url.rstrip('/')}/set/{urllib.parse.quote(key, safe='')}/{urllib.parse.quote(val_str, safe='')}"
        if ttl:
            endpoint += f"?EX={ttl}"
        req = urllib.request.Request(endpoint, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("result") == "OK"
    except Exception:
        return False


async def cache_get(url: str, opts_hash: str = "") -> Optional[dict]:
    """
    Try: Upstash REST (persistent) -> Redis TCP -> memory
    """
    key = _key_for(url, opts_hash)
    cache_key = f"toolz:cache:{key}"

    # 1) Upstash REST (sync, but we call in async context — it's fast http)
    # Run in thread? For Vercel we can block briefly; keep simple sync call.
    up = _upstash_get(cache_key)
    if up is not None:
        return up

    # 2) Redis TCP
    r = _get_redis()
    if r is not None:
        try:
            raw = await r.get(cache_key)
            if raw:
                return json.loads(raw)
        except Exception:
            pass

    # 3) Memory
    v = mem_get(key)
    return v


async def cache_set(url: str, value: dict, ttl: int, opts_hash: str = "") -> None:
    key = _key_for(url, opts_hash)
    cache_key = f"toolz:cache:{key}"

    # Upstash (best effort)
    _upstash_set(cache_key, value, ttl)

    # Redis
    r = _get_redis()
    if r is not None:
        try:
            await r.setex(cache_key, ttl, json.dumps(value, default=str))
        except Exception:
            pass

    mem_set(key, value, ttl)


def cache_key(url: str) -> str:
    return _key_for(url)
