"""
Smoke tests for toolz-downloadz-api v2
Run: pytest -q
"""
import os
import importlib

os.environ.setdefault("API_SECRET_KEY", "test123")
os.environ.setdefault("ENVIRONMENT", "development")

import api.core.config as cfg
import api.main as main

# Reload to pick up test env
importlib.reload(cfg)
importlib.reload(main)

from fastapi.testclient import TestClient
from api.main import app

client = TestClient(app)
HEADERS = {"X-API-KEY": "test123"}


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "online"
    assert "version" in j


def test_platforms():
    r = client.get("/api/platforms")
    assert r.status_code == 200
    j = r.json()
    assert "platforms" in j
    assert len(j["platforms"]) >= 10
    ids = {p["id"] for p in j["platforms"]}
    assert "youtube" in ids
    assert "tiktok" in ids
    assert "soundcloud" in ids


def test_detect():
    r = client.get("/api/detect", params={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
    assert r.status_code == 200
    assert r.json()["platform"] == "youtube"
    r = client.get("/api/detect", params={"url": "https://soundcloud.com/edsheeran/perfect"})
    assert r.json()["platform"] == "soundcloud"
    r = client.get("/api/detect", params={"url": "https://example.com/video.mp4"})
    assert r.json()["platform"] == "generic"


def test_auth_required():
    r = client.get("/api/extract", params={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})
    assert r.status_code == 401


def test_ssrf_blocked():
    for bad in [
        "http://127.0.0.1/video",
        "http://localhost:8000/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/secret",
    ]:
        r = client.get("/api/extract", params={"url": bad}, headers=HEADERS)
        assert r.status_code == 400
        assert "not allowed" in r.json()["detail"].lower() or "invalid" in r.json()["detail"].lower()


def test_invalid_url():
    r = client.get("/api/extract", params={"url": "not-a-url"}, headers=HEADERS)
    assert r.status_code in (400, 422)
    r = client.get("/api/extract", params={"url": "ftp://example.com/video.mp4"}, headers=HEADERS)
    assert r.status_code == 400


def test_cors_headers():
    r = client.get("/api/health", headers={"Origin": "http://localhost:3000"})
    # CORS middleware adds header
    assert "access-control-allow-origin" in {k.lower() for k in r.headers.keys()}


def test_cache_second_hit():
    # This tests in-memory cache wiring (needs network for first call, but we can test via mocked URL maybe not)
    # Use youtube which we know works locally
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    r1 = client.get("/api/extract", params={"url": url}, headers=HEADERS)
    # If network works, first should be 200
    if r1.status_code == 200:
        r2 = client.get("/api/extract", params={"url": url}, headers=HEADERS)
        assert r2.status_code == 200
        assert r2.json().get("_cached") is True
    else:
        # If network fails in CI, at least it returns 400 not 500
        assert r1.status_code == 400
