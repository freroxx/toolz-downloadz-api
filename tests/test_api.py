"""Smoke tests: PYTHONPATH=. pytest -q"""
import os
os.environ.setdefault("API_SECRET_KEY", "test123")

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from api import index as api
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


# --- Offline unit tests (no network) ----------------------------------------

def _tikwm_payload(**over):
    d = {
        "id": "7677478472293289247",
        "title": "Why the friend group yt channel never works",
        "cover": "https://cdn/cover.jpg",
        "origin_cover": "https://cdn/origin.jpg",
        "play": "https://cdn/play.mp4",
        "hdplay": "https://cdn/hdplay.mp4",
        "wmplay": "https://cdn/wmplay.mp4",
        "size": 5421185,
        "hd_size": 4561873,
        "wm_size": 5025333,
        "duration": 32,
        "create_time": 1750000000,
        "music": "https://cdn/music.mp3",
        "play_count": 1000,
        "digg_count": 50,
        "comment_count": 5,
        "author": {"nickname": "carter", "unique_id": "carterpcs"},
    }
    d.update(over)
    return d


def test_tikwm_shape_hd_is_honest():
    out = api._tikwm_shape(_tikwm_payload(), TT)
    v = out["formats"]["video"][0]
    assert v["format_id"] == "tikwm_hd"
    assert v["resolution"].startswith("~")  # approximate tier, never stated as fact
    assert "1080p" not in v["resolution"] or v["resolution"].startswith("~")
    assert v["height"] is None and v["vcodec"] is None and v["acodec"] is None
    assert v["has_audio"] is True
    assert v["filesize"] == 4561873  # byte count for the exact file served
    assert out["duration"] == 32
    assert out["upload_date"] == "20250615"
    assert out["download_url"] == "https://cdn/hdplay.mp4"


def test_tikwm_shape_sd_fallback_size():
    d = _tikwm_payload(hdplay=None, hd_size=None)
    out = api._tikwm_shape(d, TT)
    v = out["formats"]["video"][0]
    assert v["format_id"] == "tikwm_sd"
    assert v["filesize"] == 5421185  # matches play, not hd
    assert out["download_url"] == "https://cdn/play.mp4"


def test_tikwm_shape_no_media_returns_none():
    assert api._tikwm_shape(_tikwm_payload(hdplay=None, play=None, wmplay=None), TT) is None


def test_tikwm_audio_only_serves_music():
    out = api._tikwm_shape(_tikwm_payload(), TT, audio_only=True)
    assert out["ext"] == "mp3"
    assert out["download_url"] == "https://cdn/music.mp3"
    assert out["formats"]["video"] == []
    assert out["formats"]["audio"][0]["format_id"] == "tikwm_music"


def test_tikwm_audio_only_without_music_keeps_video():
    out = api._tikwm_shape(_tikwm_payload(music=None), TT, audio_only=True)
    assert out["ext"] == "mp4" and out["download_url"]


def test_slideshow_raises_clean_error(monkeypatch):
    monkeypatch.setattr(api, "_tikwm_fetch",
                        lambda url: {"images": ["https://cdn/1.jpg"], "title": "pics"})
    with pytest.raises(RuntimeError, match="slideshow"):
        api.extract_sync("https://www.tiktok.com/@u/video/123")


def test_client_ip_prefers_forwarded_for():
    req = SimpleNamespace(headers={"x-forwarded-for": "1.2.3.4, 5.6.7.8"}, client=None)
    assert api.client_ip(req) == "1.2.3.4"
    direct = SimpleNamespace(headers={}, client=SimpleNamespace(host="9.9.9.9"))
    assert api.client_ip(direct) == "9.9.9.9"


def test_key_url_strips_tracking_params():
    a = "https://www.tiktok.com/@u/video/123?is_from_webapp=1&sender_device=pc"
    b = "https://www.tiktok.com/@u/video/123?foo=bar#frag"
    assert api._key_url(a) == api._key_url(b) == "https://www.tiktok.com/@u/video/123"


def test_int_env_falls_back(monkeypatch):
    monkeypatch.setenv("EXTRACT_TIMEOUT", "not-a-number")
    assert api._int_env("EXTRACT_TIMEOUT", 26) == 26
    monkeypatch.setenv("EXTRACT_TIMEOUT", "10")
    assert api._int_env("EXTRACT_TIMEOUT", 26) == 10


def test_post_empty_body_reports_missing_url():
    r = c.post("/api/extract", json={}, headers=H)
    assert r.status_code == 400
    assert "Missing 'url'" in r.json()["detail"]
