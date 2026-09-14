"""Smoke tests: PYTHONPATH=. pytest -q"""
import os
os.environ.setdefault("API_SECRET_KEY", "test123")

from fastapi.testclient import TestClient
from api.index import app, detect_platform

c = TestClient(app)
H = {"X-API-KEY": "test123"}
TT = "https://www.tiktok.com/@carterpcs/video/7677478472293289247?is_from_webapp=1&sender_device=pc"
IG = "https://www.instagram.com/reel/C3JhP8vLq5G/"


def test_health():
    r = c.get("/api/health")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "online" and j["version"].startswith("4.")
    assert set(j["platforms"]) == {"tiktok", "instagram"}


def test_platforms():
    ids = {p["id"] for p in c.get("/api/platforms").json()["platforms"]}
    assert ids == {"tiktok", "instagram"}


def test_detect_platform():
    assert detect_platform(TT) == "tiktok"
    assert detect_platform(IG) == "instagram"
    assert detect_platform("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is None
    assert detect_platform("https://vimeo.com/123456") is None


def test_auth_required():
    assert c.get("/api/extract", params={"url": TT}).status_code == 401


def test_unsupported_platform():
    for bad in ("https://vimeo.com/123456",
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ"):
        r = c.get("/api/extract", params={"url": bad}, headers=H)
        assert r.status_code == 400
        assert "Only TikTok" in r.json()["detail"]


def test_ssrf_blocked():
    assert c.get("/api/extract", params={"url": "http://127.0.0.1/x"}, headers=H).status_code == 400
    assert c.get("/api/extract", params={"url": "http://169.254.169.254/x"}, headers=H).status_code == 400


def test_tiktok_never_400():
    """TikTok may be intermittently bot-blocked, but must always return 200 w/ metadata or links."""
    r = c.get("/api/extract", params={"url": TT}, headers=H)
    assert r.status_code == 200
    j = r.json()
    assert j["platform"] == "tiktok"
    if j["blocked"]:
        assert j["blocked_message"]
    else:
        assert j["download_url"]


def test_instagram_graceful_error():
    """Without cookies IG fails — must be a clean 400 with cookie guidance."""
    r = c.get("/api/extract", params={"url": IG}, headers=H)
    if r.status_code == 200:
        assert r.json()["platform"] == "instagram"
    else:
        assert r.status_code == 400
        assert "INSTAGRAM_COOKIES" in r.json()["detail"]


def test_cache_hit():
    r1 = c.get("/api/extract", params={"url": TT}, headers=H)
    if r1.status_code == 200 and not r1.json().get("blocked"):
        r2 = c.get("/api/extract", params={"url": TT}, headers=H)
        assert r2.json().get("_cached") is True


def test_post_extract():
    r = c.post("/api/extract", json={"url": TT}, headers=H)
    assert r.status_code == 200


def test_audio_only_flag():
    r = c.get("/api/extract", params={"url": TT, "audio_only": "true"}, headers=H)
    assert r.status_code in (200, 504)
