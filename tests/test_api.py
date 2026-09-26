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
    assert j["status"] == "online" and j["version"].startswith("5.")
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


def test_slideshow_becomes_ordered_gallery(monkeypatch):
    monkeypatch.setattr(api, "_tikwm_fetch",
                        lambda url: {"images": ["https://cdn/1.jpg"], "title": "pics"})
    out = api.extract_sync("https://www.tiktok.com/@u/video/123")
    assert out["gallery"][0]["format_id"] == "gallery:0"


def test_v1_detects_only_real_registered_domains():
    assert api.detect_v1_platform("https://youtu.be/dQw4w9WgXcQ") == "youtube"
    assert api.detect_v1_platform("https://not-tiktok.com/@u/video/1") is None
    assert api.detect_v1_platform("https://instagram.com.evil.test/reel/x") is None


def test_v1_session_and_extraction_hide_upstream_urls(monkeypatch):
    session = c.post("/api/v1/client-sessions", json={"installation_id": "device-identifier-1234"})
    assert session.status_code == 200
    token = session.json()["access_token"]
    fake = api._tikwm_shape(_tikwm_payload(), TT)
    monkeypatch.setattr(api, "extract_v1_sync", lambda url, audio_only=False: fake)
    response = c.post("/api/v1/extractions", json={"url": TT}, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["assets"] and all("url" not in asset for asset in payload["assets"])
    assert payload["assets"][0]["download_path"].startswith("/api/v1/extractions/")


def test_v1_rejects_missing_or_expired_session():
    r = c.post("/api/v1/extractions", json={"url": TT})
    assert r.status_code == 401


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


# --- Quality ladder: dedupe, fragments, 1080p default ------------------------

def _ydl_fmt(fid, w, h, vc="avc1", ac="mp4a", size=None, tbr=None, **over):
    f = {"format_id": fid, "ext": "mp4", "url": f"https://cdn/{fid}.mp4",
         "resolution": f"{w}x{h}", "width": w, "height": h,
         "vcodec": vc, "acodec": ac, "filesize": size,
         "filesize_approx": None, "tbr": tbr, "abr": None, "fps": 30,
         "protocol": "https", "http_headers": {}, "cookies": None}
    f.update(over)
    return f


def test_dedupe_collapses_identical_specs():
    info = {"formats": [
        _ydl_fmt("0", 720, 1280, size=1000),
        _ydl_fmt("1", 720, 1280, size=1000),   # IG duplicate encode
        _ydl_fmt("2", 1080, 1920, size=2000),
    ]}
    video, _ = api.normalize(info)
    assert [(v["width"], v["height"]) for v in video] == [(1080, 1920), (720, 1280)]


def test_dedupe_keeps_distinct_codecs():
    info = {"formats": [
        _ydl_fmt("h264_720p-0", 720, 1280, vc="h264", size=5000, tbr=1324),
        _ydl_fmt("bytevc1_720p-0", 720, 1280, vc="h265", size=2600, tbr=683),
    ]}
    video, _ = api.normalize(info)
    assert len(video) == 2  # same dims, different codec = real choice


def test_tiktok_pair_suffixes_deduped():
    info = {"formats": [
        _ydl_fmt("h264_720p_1324906-0", 720, 1280, vc="h264", size=5170000, tbr=1324),
        _ydl_fmt("h264_720p_1324906-1", 720, 1280, vc="h264", size=5170000, tbr=1324),
    ]}
    video, _ = api.normalize(info)
    assert len(video) == 1


def test_fragments_and_watermarked_dropped():
    info = {"formats": [
        _ydl_fmt("dash-v", 720, 1280, fragments=[{"url": "x"}]),
        _ydl_fmt("download", 720, 1280, format_note="Untested, watermarked"),
        _ydl_fmt("h264_720p-0", 720, 1280, vc="h264", size=1000),
    ]}
    video, _ = api.normalize(info)
    assert [v["format_id"] for v in video] == ["h264_720p-0"]


def test_best_prefers_1080p():
    info = {"title": "t", "formats": [
        _ydl_fmt("720p", 720, 1280, size=9000, tbr=5000),   # higher bitrate, lower res
        _ydl_fmt("1080p", 1080, 1920, size=4000, tbr=1114),
        _ydl_fmt("540p", 576, 1024, size=2000, tbr=577),
    ]}
    out = api.shape("instagram", info, IG)
    assert out["download_url"] == "https://cdn/1080p.mp4"


def test_best_falls_back_to_highest_without_1080():
    info = {"title": "t", "formats": [
        _ydl_fmt("480p", 480, 854, size=1000, tbr=400),
        _ydl_fmt("720p", 720, 1280, size=3000, tbr=900),
    ]}
    out = api.shape("instagram", info, IG)
    assert out["download_url"] == "https://cdn/720p.mp4"


def test_ladder_merges_exact_rows_and_keeps_hd_default(monkeypatch):
    fast = api._tikwm_shape(_tikwm_payload(), TT)
    ydl_info = {"title": "t", "formats": [
        _ydl_fmt("h264_720p_1324906-0", 720, 1280, vc="h264", size=5170000, tbr=1324),
        _ydl_fmt("h264_720p_1324906-1", 720, 1280, vc="h264", size=5170000, tbr=1324),
        _ydl_fmt("bytevc1_1080p_1114895-0", 1080, 1920, vc="h265", size=4561000, tbr=1114),
        _ydl_fmt("download", 720, 1280, format_note="Untested, watermarked"),
    ]}
    monkeypatch.setattr(api, "run_ydl", lambda opts, url: ydl_info)
    out = api.tiktok_ladder(TT, False, fast)
    assert out["ladder"] == "full"
    assert out["download_url"] == "https://cdn/hdplay.mp4"  # default stays IP-free HD
    vids = out["formats"]["video"]
    assert [v["format_id"] for v in vids] == ["bytevc1_1080p_1114895-0", "h264_720p_1324906-0"]
    assert all(v["ladder"] == "full" and v["ip_free"] is False for v in vids)
    assert all("watermark" not in (v.get("resolution") or "").lower() for v in vids)


def test_ladder_failure_degrades_to_fast(monkeypatch):
    fast = api._tikwm_shape(_tikwm_payload(), TT)
    monkeypatch.setattr(api, "run_ydl", lambda opts, url: (_ for _ in ()).throw(Exception("blocked")))
    out = api.tiktok_ladder(TT, False, fast)
    assert out["ladder"] == "fast" and "ladder_error" in out
    assert out["download_url"] == fast["download_url"]


def test_extract_auto_merges_full_ladder(monkeypatch):
    monkeypatch.setattr(api, "_tikwm_fetch", lambda url: _tikwm_payload())
    ydl_info = {"title": "t", "formats": [
        _ydl_fmt("h264_720p_1324906-0", 720, 1280, vc="h264", size=5170000, tbr=1324),
        _ydl_fmt("bytevc1_1080p_1114895-0", 1080, 1920, vc="h265", size=4561000, tbr=1114),
    ]}
    monkeypatch.setattr(api, "run_ydl", lambda opts, url: ydl_info)
    out = api.extract_sync(TT)
    assert out["ladder"] == "full"
    assert [v["format_id"] for v in out["formats"]["video"]] == [
        "bytevc1_1080p_1114895-0", "h264_720p_1324906-0"]
    assert out["download_url"] == "https://cdn/hdplay.mp4"  # default stays 1080p HD
