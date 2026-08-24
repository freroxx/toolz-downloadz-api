"""Smoke tests: PYTHONPATH=. pytest -q"""
import os
os.environ.setdefault("API_SECRET_KEY", "test123")

from fastapi.testclient import TestClient
from api.index import app

c = TestClient(app)
H = {"X-API-KEY": "test123"}
YT = "https://www.youtube.com/watch?v=Ec9FSxwPea0"
TT = "https://www.tiktok.com/@carterpcs/video/7677478472293289247?is_from_webapp=1&sender_device=pc"


def test_health():
    r = c.get("/api/health")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "online" and j["version"].startswith("3.")
    assert set(j["platforms"]) == {"youtube", "tiktok", "instagram"}


def test_platforms():
    ids = {p["id"] for p in c.get("/api/platforms").json()["platforms"]}
    assert ids == {"youtube", "tiktok", "instagram"}


def test_auth_required():
    assert c.get("/api/extract", params={"url": YT}).status_code == 401


def test_unsupported_platform():
    r = c.get("/api/extract", params={"url": "https://vimeo.com/123456"}, headers=H)
    assert r.status_code == 400
    assert "Only YouTube" in r.json()["detail"]


def test_ssrf_blocked():
    assert c.get("/api/extract", params={"url": "http://127.0.0.1/x"}, headers=H).status_code == 400
    assert c.get("/api/extract", params={"url": "http://169.254.169.254/x"}, headers=H).status_code == 400


def test_youtube_extract():
    r = c.get("/api/extract", params={"url": YT}, headers=H)
    assert r.status_code == 200
    j = r.json()
    assert j["platform"] == "youtube" and j["title"]
    if not j["blocked"]:
        assert j["download_url"] and j["formats"]["video"]


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
    r = c.get("/api/extract", params={"url": "https://www.instagram.com/reel/C3JhP8vLq5G/"}, headers=H)
    if r.status_code == 200:
        assert r.json()["platform"] == "instagram"
    else:
        assert r.status_code == 400
        assert "INSTAGRAM_COOKIES" in r.json()["detail"]


def test_cache_hit():
    r1 = c.get("/api/extract", params={"url": YT}, headers=H)
    if r1.status_code == 200 and not r1.json().get("blocked"):
        r2 = c.get("/api/extract", params={"url": YT}, headers=H)
        assert r2.json().get("_cached") is True


def test_post_extract():
    r = c.post("/api/extract", json={"url": YT}, headers=H)
    assert r.status_code == 200


def test_audio_only_flag():
    r = c.get("/api/extract", params={"url": YT, "audio_only": "true"}, headers=H)
    assert r.status_code in (200, 504)
