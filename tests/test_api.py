"""Smoke tests: PYTHONPATH=. pytest -q"""
import os
os.environ.setdefault("API_SECRET_KEY", "test123")

from types import SimpleNamespace

import json
import time
import re
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


# --- YouTube cookie wiring (offline) ----------------------------------------
YT_COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1999999999\tVISITOR_INFO1_LIVE\tabc123\n"
    ".youtube.com\tTRUE\t/\tTRUE\t1999999999\tLOGIN_INFO\tdef456:xyz\n"
)


def test_youtube_opts_use_cookiefile_when_configured(monkeypatch):
    monkeypatch.setattr(api, "YOUTUBE_COOKIES", YT_COOKIES)
    opts = api.ydl_opts("youtube")
    assert "cookiefile" in opts
    assert opts["cookiefile"].endswith("yt_cookies.txt")
    with open(opts["cookiefile"], encoding="utf-8") as f:
        content = f.read()
    assert "VISITOR_INFO1_LIVE" in content and "LOGIN_INFO" in content
    # Fallback chain so DASH-only videos (no muxed file) don't hard-fail.
    assert opts["format"] == "best/bestvideo+bestaudio"
    # android needs a PO token and web is SABR-only: neither can yield a stream here.
    assert opts["extractor_args"]["youtube"]["player_client"] == ["android_vr", "tv", "mweb"]


def test_youtube_opts_skip_cookiefile_when_unset(monkeypatch):
    monkeypatch.setattr(api, "YOUTUBE_COOKIES", "")
    opts = api.ydl_opts("youtube")
    assert "cookiefile" not in opts
    assert opts["format"] == "best/bestvideo+bestaudio"


def test_youtube_v1_detection_covers_watch_shorts_music():
    for u in (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
    ):
        assert api.detect_v1_platform(u) == "youtube", u
    assert api.detect_v1_platform("https://vimeo.com/123456") is None


# --- YouTube dead-link vs refusal distinction (offline) ----------------------
def test_youtube_id_shapes():
    assert api._youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert api._youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert api._youtube_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert api._youtube_id("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert api._youtube_id("https://vimeo.com/123456") is None
    assert api._youtube_id("not a url") is None


def test_youtube_dead_link_says_unavailable(monkeypatch):
    monkeypatch.setattr(api, "run_ydl", lambda opts, url: (_ for _ in ()).throw(
        Exception("[youtube] cx0mMqqF8lQ: Requested format is not available")))
    monkeypatch.setattr(api, "_oembed", lambda endpoint: None)
    with pytest.raises(RuntimeError, match="doesn't exist"):
        api.extract_v1_sync("https://www.youtube.com/watch?v=cx0mMqqF8lQ")


def test_youtube_refusal_keeps_original_error_when_video_exists(monkeypatch):
    monkeypatch.setattr(api, "run_ydl", lambda opts, url: (_ for _ in ()).throw(
        Exception("[youtube] dQw4w9WgXcQ: Requested format is not available")))
    monkeypatch.setattr(api, "_oembed", lambda endpoint: {"title": "t"})
    with pytest.raises(RuntimeError, match="YouTube extraction failed"):
        api.extract_v1_sync("https://www.youtube.com/watch?v=dQw4w9WgXcQ")


# --- toolz-ytapi fallback chain (offline) -----------------------------------
YT_CONTRACT = {
    "title": "Never Gonna Give You Up",
    "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hq.jpg",
    "duration": 213,
    "uploader": "Rick Astley",
    "uploader_url": "https://www.youtube.com/@RickAstleyYT",
    "view_count": 1000,
    "like_count": 50,
    "comment_count": 5,
    "upload_date": "20091024",
    "description": "The official video",
    "formats": [
        {"format_id": "18", "itag": 18, "kind": "progressive", "ext": "mp4",
         "mime": 'video/mp4; codecs="avc1.42001E, mp4a.40.2"', "width": 640, "height": 360,
         "bitrate": 500000, "content_length": 5000000, "quality_label": "360p",
         "is_audio": False, "has_audio": True, "fps": 30},
        {"format_id": "137", "itag": 137, "kind": "video", "ext": "mp4",
         "mime": 'video/mp4; codecs="avc1.640028"', "width": 1920, "height": 1080,
         "bitrate": 4500000, "content_length": 90000000, "quality_label": "1080p",
         "is_audio": False, "has_audio": False, "fps": 30},
        {"format_id": "313", "itag": 313, "kind": "video", "ext": "webm",
         "mime": 'video/webm; codecs="vp9"', "width": 3840, "height": 2160,
         "bitrate": 12000000, "content_length": 300000000, "quality_label": "2160p",
         "is_audio": False, "has_audio": False, "fps": 30},
        {"format_id": "140", "itag": 140, "kind": "audio", "ext": "mp4",
         "mime": 'audio/mp4; codecs="mp4a.40.2"', "bitrate": 128000,
         "content_length": 3400000, "is_audio": True, "has_audio": False},
        {"format_id": "137+140", "itag": "137+140", "kind": "merged", "ext": "mp4",
         "mime": "video/mp4", "width": 1920, "height": 1080, "bitrate": 4628000,
         "content_length": 93400000, "quality_label": "1080p", "merged": True,
         "is_audio": False, "has_audio": True, "fps": 30},
    ],
}
YT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _no_formats(url):
    raise Exception("[youtube] dQw4w9WgXcQ: Requested format is not available")


def test_youtube_shape_prefers_sound_and_hides_redundant_silent_rows():
    out = api.youtube_shape(YT_CONTRACT, YT_URL)
    assert out["platform"] == "youtube" and out["source"] == "ytapi" and out["video_id"] == "dQw4w9WgXcQ"
    rows = out["formats"]["video"]
    by_h = {r["height"]: r for r in rows}
    assert list(by_h) == [2160, 1080, 360]
    assert by_h[1080]["format_id"] == "137+140" and by_h[1080]["has_audio"] is True
    assert by_h[2160]["has_audio"] is False  # only offered silent: said so, not hidden
    assert by_h[360]["has_audio"] is True
    assert out["best_format"] == "137+140"  # 1080p with sound, never the silent 2160 track
    assert out["download_url"] is None
    assert all(r["url"].startswith("ytapi://") for r in rows)  # IP-bound URLs never held here
    assert out["formats"]["audio"][0]["ext"] == "m4a" and out["formats"]["audio"][0]["acodec"] == "mp4a"
    assert out["stats"]["view_count"] == 1000


def test_youtube_shape_without_mux_never_defaults_to_a_silent_track_when_sound_exists():
    contract = dict(YT_CONTRACT, formats=[r for r in YT_CONTRACT["formats"] if not r.get("merged")])
    out = api.youtube_shape(contract, YT_URL)
    assert out["best_format"] == "18"  # 360p WITH sound beats silent 1080p
    only_silent = dict(YT_CONTRACT, formats=[r for r in YT_CONTRACT["formats"] if r["kind"] in ("video", "audio")])
    assert api.youtube_shape(only_silent, YT_URL)["best_format"] == "137"  # honest: nothing better exists


def test_youtube_shape_audio_only_and_empty():
    out = api.youtube_shape(YT_CONTRACT, YT_URL, audio_only=True)
    assert out["best_format"] == "140" and out["best_group"] == "audio" and out["ext"] == "m4a"
    with pytest.raises(RuntimeError, match="no streams"):
        api.youtube_shape({"formats": [{"nope": 1}]}, YT_URL)


def test_ytapi_is_primary_and_ytdlp_is_not_run_on_success(monkeypatch):
    monkeypatch.setattr(api, "YT_EXTRACT_URL", "https://ytapi.example")
    monkeypatch.setattr(api, "_ytapi_fetch", lambda url: YT_CONTRACT)
    monkeypatch.setattr(api, "run_ydl", lambda *a: pytest.fail("yt-dlp must not burn the time budget first"))
    out = api.extract_v1_sync(YT_URL)
    assert out["source"] == "ytapi" and out["best_format"] == "137+140"


def test_ytdlp_is_the_fallback_when_ytapi_fails(monkeypatch):
    monkeypatch.setattr(api, "YT_EXTRACT_URL", "https://ytapi.example")
    monkeypatch.setattr(api, "_ytapi_fetch", lambda url: (_ for _ in ()).throw(
        RuntimeError("extraction service: failed: No client returned playable streams")))
    monkeypatch.setattr(api, "run_ydl", lambda opts, url: {
        "title": "t", "formats": [], "url": "https://cdn/v.mp4", "ext": "mp4", "http_headers": {}})
    out = api.extract_v1_sync(YT_URL)
    assert out["download_url"] == "https://cdn/v.mp4" and out.get("source") != "ytapi"


def test_both_failing_reports_the_service_verdict_and_ytdlp_text(monkeypatch):
    monkeypatch.setattr(api, "YT_EXTRACT_URL", "https://ytapi.example")
    monkeypatch.setattr(api, "_ytapi_fetch", lambda url: (_ for _ in ()).throw(
        RuntimeError("extraction service: failed: ANDROID_VR: googlevideo 403")))
    monkeypatch.setattr(api, "run_ydl", lambda opts, url: _no_formats(url))
    with pytest.raises(RuntimeError) as e:
        api.extract_v1_sync(YT_URL)
    assert "googlevideo 403" in str(e.value) and "Requested format" in str(e.value)


@pytest.mark.parametrize("code,expect", [
    ("private", "private"), ("age", "age-restricted"), ("region", "region"),
    ("unavailable", "doesn't exist"),
])
def test_definitive_verdicts_skip_ytdlp(monkeypatch, code, expect):
    monkeypatch.setattr(api, "YT_EXTRACT_URL", "https://ytapi.example")
    monkeypatch.setattr(api, "_ytapi_fetch", lambda url: (_ for _ in ()).throw(
        RuntimeError(f"extraction service: {code}: detail")))
    monkeypatch.setattr(api, "run_ydl", lambda *a: pytest.fail("a verdict can't be changed by yt-dlp"))
    with pytest.raises(RuntimeError, match=expect):
        api.extract_v1_sync(YT_URL)


def test_ytdlp_fallback_is_skipped_once_the_time_budget_is_spent(monkeypatch):
    clock = iter([0.0, 40.0, 40.0, 40.0])
    monkeypatch.setattr(api.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(api, "YT_EXTRACT_URL", "https://ytapi.example")
    monkeypatch.setattr(api, "_ytapi_fetch", lambda url: (_ for _ in ()).throw(
        RuntimeError("extraction service unreachable: timed out")))
    monkeypatch.setattr(api, "run_ydl", lambda *a: pytest.fail("no time left for a second attempt"))
    with pytest.raises(RuntimeError, match="time budget"):
        api.extract_v1_sync(YT_URL)


def test_no_ytapi_url_means_ytdlp_only_with_android_vr(monkeypatch):
    monkeypatch.setattr(api, "YT_EXTRACT_URL", "")
    seen = {}
    def fake(opts, url):
        seen.update(opts)
        return {"title": "t", "formats": [], "url": "https://cdn/v.mp4", "ext": "mp4", "http_headers": {}}
    monkeypatch.setattr(api, "run_ydl", fake)
    api.extract_v1_sync(YT_URL)
    assert seen["extractor_args"]["youtube"]["player_client"][0] == "android_vr"


# --- signed download tickets + redirect (IP-bound googlevideo URLs) ----------
def _verify_ticket_like_node(secret, ticket, now=None):
    """Independent re-implementation of toolz-ytapi/lib/ticket.js verifyTicket."""
    import base64, hashlib, hmac, json as _json
    body, _, sig = ticket.partition(".")
    want = base64.urlsafe_b64encode(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    assert hmac.compare_digest(sig, want)
    payload = _json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert payload["e"] > (now or time.time())
    assert re.fullmatch(r"[A-Za-z0-9_-]{11}", payload["v"]) and re.fullmatch(r"\d+(\+\d+)?", payload["f"])
    return payload


def test_ticket_is_signed_short_lived_and_unpadded(monkeypatch):
    monkeypatch.setattr(api, "YT_EXTRACT_URL", "https://ytapi.example")
    monkeypatch.setattr(api, "YTAPI_SECRET", "s3cret")
    url = api._ytapi_download_url("dQw4w9WgXcQ", "137+140", "Rick Roll.mp4", 1080)
    assert url.startswith("https://ytapi.example/api/extract?t=")
    ticket = url.split("t=", 1)[1]
    assert "=" not in ticket and "+" not in ticket and "/" not in ticket
    payload = _verify_ticket_like_node("s3cret", ticket)
    assert payload["v"] == "dQw4w9WgXcQ" and payload["f"] == "137+140" and payload["h"] == 1080
    assert payload["n"] == "Rick Roll.mp4"
    assert 0 < payload["e"] - time.time() <= api.YT_TICKET_TTL + 2
    with pytest.raises(AssertionError):
        _verify_ticket_like_node("wrong", ticket)


def test_ticket_requires_service_configuration(monkeypatch):
    monkeypatch.setattr(api, "YT_EXTRACT_URL", "https://ytapi.example")
    monkeypatch.setattr(api, "YTAPI_SECRET", "")
    with pytest.raises(api.HTTPException) as e:
        api._ytapi_download_url("dQw4w9WgXcQ", "18", "x.mp4")
    assert e.value.status_code == 503


def test_ytapi_assets_download_via_307_to_the_service_and_never_touch_the_api_network(monkeypatch):
    monkeypatch.setattr(api, "YT_EXTRACT_URL", "https://ytapi.example")
    monkeypatch.setattr(api, "YTAPI_SECRET", "s3cret")
    token = c.post("/api/v1/client-sessions", json={"installation_id": "device-identifier-1234"}).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(api, "extract_v1_sync", lambda url, audio_only=False: api.youtube_shape(YT_CONTRACT, url, audio_only))
    monkeypatch.setattr(api, "_stream_download", lambda *a, **k: pytest.fail("YouTube bytes must not flow through this API"))
    payload = c.post("/api/v1/extractions", json={"url": YT_URL}, headers=auth).json()
    assets = {a["id"]: a for a in payload["assets"]}
    assert all("url" not in a and "ytapi://" not in json.dumps(a) for a in payload["assets"])
    best = assets["best"]
    assert best["has_audio"] is True and best["height"] == 1080 and best["mime_type"] == "video/mp4"
    r = c.get(best["download_path"], headers=auth, follow_redirects=False)
    assert r.status_code == 307 and r.headers["cache-control"] == "no-store"
    ticket = r.headers["location"].split("t=", 1)[1]
    p = _verify_ticket_like_node("s3cret", ticket)
    assert p["f"] == "137+140" and p["h"] == 1080 and p["n"].endswith(".mp4")
    audio = next(a for a in payload["assets"] if a["kind"] == "audio")
    r2 = c.get(audio["download_path"], headers=auth, follow_redirects=False)
    assert _verify_ticket_like_node("s3cret", r2.headers["location"].split("t=", 1)[1])["f"] == "140"


def test_ytapi_fetch_requests_contract_v2(monkeypatch):
    monkeypatch.setattr(api, "YT_EXTRACT_URL", "https://ytapi.example")
    sent = {}
    def fake(req, timeout=0):
        sent["body"] = json.loads(req.data)
        sent["timeout"] = timeout
        return _FakeResp({"ok": True, "data": YT_CONTRACT})
    monkeypatch.setattr(api.urllib.request, "urlopen", fake)
    api._ytapi_fetch(YT_URL)
    assert sent["body"] == {"url": YT_URL, "v": 2} and sent["timeout"] <= 35


def test_ytapi_http_error_body_keeps_the_structured_verdict(monkeypatch):
    import io
    monkeypatch.setattr(api, "YT_EXTRACT_URL", "https://ytapi.example")
    def fake(req, timeout=0):
        raise api.urllib.error.HTTPError(req.full_url, 422, "Unprocessable", {},
                                         io.BytesIO(b'{"ok":false,"error":"private","detail":"Private video"}'))
    monkeypatch.setattr(api.urllib.request, "urlopen", fake)
    with pytest.raises(RuntimeError, match="extraction service: private"):
        api._ytapi_fetch(YT_URL)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        import json as _json
        return _json.dumps(self._payload).encode()


def test_ytapi_fetch_contract_and_errors(monkeypatch):
    monkeypatch.setattr(api, "YT_EXTRACT_URL", "https://ytapi.example")
    monkeypatch.setattr(api.urllib.request, "urlopen",
                         lambda req, timeout=20: _FakeResp({"ok": True, "data": YT_CONTRACT}))
    assert api._ytapi_fetch(YT_URL)["title"] == "Never Gonna Give You Up"

    monkeypatch.setattr(api.urllib.request, "urlopen",
                         lambda req, timeout=20: _FakeResp(
                             {"ok": False, "error": "private", "detail": "Private video"}))
    with pytest.raises(RuntimeError, match="private"):
        api._ytapi_fetch(YT_URL)

    def _boom(req, timeout=20):
        raise OSError("conn reset")
    monkeypatch.setattr(api.urllib.request, "urlopen", _boom)
    with pytest.raises(RuntimeError, match="unreachable"):
        api._ytapi_fetch(YT_URL)


# --- ytapi structured errors + GVS recheck (offline) -------------------------
def test_ytapi_primary_error_mapping():
    assert str(api._ytapi_primary_error(
        "extraction service: private: Private video", "wall")) == \
        "This YouTube video is private."
    assert "age-restricted" in str(api._ytapi_primary_error(
        "extraction service: age: Sign-in required", "wall"))
    assert "region" in str(api._ytapi_primary_error(
        "extraction service: region: Blocked", "wall"))
    assert "re-export YOUTUBE_COOKIES" in str(api._ytapi_primary_error(
        "extraction service: login: bot wall", "wall"))
    generic = str(api._ytapi_primary_error("extraction service unreachable: boom", "wall msg"))
    assert "wall msg" in generic and "boom" in generic


def test_structured_login_becomes_primary_error(monkeypatch):
    monkeypatch.setattr(api, "YT_EXTRACT_URL", "https://ytapi.example")
    monkeypatch.setattr(api, "run_ydl", lambda opts, url: _no_formats(url))
    monkeypatch.setattr(api, "_ytapi_fetch",
                         lambda url: (_ for _ in ()).throw(
                             RuntimeError("extraction service: login: bot wall")))
    with pytest.raises(RuntimeError, match="re-export YOUTUBE_COOKIES"):
        api.extract_v1_sync(YT_URL)


class _HeadResp(_FakeResp):
    def __init__(self, payload, status=200):
        super().__init__(payload)
        self.status = status


# --- shape(): split-pair ranking (DASH-only YouTube) -------------------------
def test_shape_ignores_split_pair_prefers_muxed():
    v1080 = _ydl_fmt("v1080", 1920, 1080, vc="avc1", ac="none", size=9000)
    a128 = _ydl_fmt("a128", 0, 0, vc="none", ac="mp4a", size=1000)
    mux720 = _ydl_fmt("mux720", 1280, 720, vc="avc1", ac="mp4a", size=5000)
    info = {"title": "t", "formats": [mux720, v1080, a128],
            "requested_formats": [v1080, a128]}
    out = api.shape("youtube", info, YT_URL)
    assert out["download_url"] == "https://cdn/mux720.mp4"  # with sound, not silent 1080p


def test_shape_dash_only_defaults_to_best_video_row():
    v1080 = _ydl_fmt("v1080", 1920, 1080, vc="avc1", ac="none", size=9000)
    v720 = _ydl_fmt("v720", 1280, 720, vc="avc1", ac="none", size=5000)
    a128 = _ydl_fmt("a128", 0, 0, vc="none", ac="mp4a", size=1000)
    info = {"title": "t", "formats": [v1080, v720, a128],
            "requested_formats": [v1080, a128]}
    out = api.shape("youtube", info, YT_URL)
    assert out["download_url"] == "https://cdn/v1080.mp4"
    assert len(out["formats"]["video"]) == 2 and len(out["formats"]["audio"]) == 1


def test_shape_single_requested_file_still_direct():
    mux = _ydl_fmt("mux", 1280, 720, size=4000)
    info = {"title": "t", "formats": [mux], "requested_formats": [mux]}
    out = api.shape("tiktok", info, TT)
    assert out["download_url"] == "https://cdn/mux.mp4"


# --- TikTok additions (offline) ----------------------------------------------
def test_tikwm_offers_a_smaller_sd_file_beside_hd_without_changing_the_default():
    out = api._tikwm_shape(_tikwm_payload(), TT)
    assert [v["format_id"] for v in out["formats"]["video"]] == ["tikwm_hd", "tikwm_sd"]
    sd = out["formats"]["video"][1]
    assert sd["url"] == "https://cdn/play.mp4" and sd["filesize"] == 5421185 and sd["has_audio"] is True
    assert out["download_url"] == "https://cdn/hdplay.mp4"  # default unchanged for every client
    same = api._tikwm_shape(_tikwm_payload(play="https://cdn/hdplay.mp4"), TT)
    assert [v["format_id"] for v in same["formats"]["video"]] == ["tikwm_hd"]  # no duplicate rung


def test_slideshow_exposes_its_soundtrack_as_audio():
    out = api.tiktok_gallery_shape({"images": ["https://cdn/1.jpg"], "music": "https://cdn/m.mp3", "title": "t"}, TT)
    assert out["formats"]["audio"][0]["format_id"] == "tikwm_music"
    assert api.tiktok_gallery_shape({"images": ["https://cdn/1.jpg"]}, TT)["formats"]["audio"] == []


# --- merging split tracks (Instagram DASH, YouTube fallback) -----------------
def _split_result(base="https://cdn"):
    return {
        "platform": "instagram", "title": "reel", "ext": "mp4", "download_url": f"{base}/p720.mp4",
        "download_headers": {}, "download_cookies": None,
        "formats": {
            "video": [
                {"format_id": "dash-v1080", "ext": "mp4", "resolution": "1080x1920", "url": f"{base}/v.mp4",
                 "filesize": 4000, "vcodec": "avc1", "acodec": "none", "has_audio": None,
                 "height": 1080, "width": 608, "tbr": 3000, "fps": 30, "headers": {}, "cookies": None},
                {"format_id": "prog-720", "ext": "mp4", "resolution": "720x1280", "url": f"{base}/p720.mp4",
                 "filesize": 2000, "vcodec": "avc1", "acodec": "mp4a", "has_audio": None,
                 "height": 720, "width": 405, "tbr": 1500, "fps": 30, "headers": {}, "cookies": None},
                {"format_id": "dash-v540", "ext": "mp4", "resolution": "540x960", "url": f"{base}/v540.mp4",
                 "filesize": 1000, "vcodec": "avc1", "acodec": "none", "has_audio": None,
                 "height": 540, "width": 304, "tbr": 900, "fps": 30, "headers": {}, "cookies": None},
            ],
            "audio": [
                {"format_id": "dash-a", "ext": "m4a", "resolution": "audio", "url": f"{base}/a.m4a",
                 "filesize": 500, "vcodec": "none", "acodec": "mp4a", "abr": 128, "headers": {}, "cookies": None},
            ],
        },
    }


FFMPEG_FOR_TESTS = os.getenv("FFMPEG_PATH") or api.shutil.which("ffmpeg")


@pytest.fixture
def with_ffmpeg(monkeypatch):
    if not FFMPEG_FOR_TESTS:
        pytest.skip("no ffmpeg available")
    monkeypatch.setenv("FFMPEG_PATH", FFMPEG_FOR_TESTS)
    monkeypatch.setattr(api, "_FFMPEG", "")
    yield
    monkeypatch.setattr(api, "_FFMPEG", "")


def test_mux_pairs_only_exist_with_ffmpeg_and_only_for_silent_top_tiers(monkeypatch, with_ffmpeg):
    pairs = api._mux_pairs(_split_result())
    # 1080 and 540 are silent-only tracks; 720 already has sound, so no pair for it.
    assert [(p["height"], p["format_id"]) for p in pairs] == [
        (1080, "mux:dash-v1080+dash-a"), (540, "mux:dash-v540+dash-a")]
    assert pairs[0]["has_audio"] is True and pairs[0]["filesize"] == 4500 and pairs[0]["ext"] == "mp4"
    monkeypatch.setenv("FFMPEG_PATH", "/nonexistent/ffmpeg")
    monkeypatch.setattr(api, "_FFMPEG", "")
    assert api._mux_pairs(_split_result()) == []


def test_mux_pairs_never_cross_container_families_or_touch_ytapi_and_galleries(with_ffmpeg):
    r = _split_result()
    r["formats"]["video"][0]["ext"] = "webm"  # webm video has no webm audio here
    assert [p["height"] for p in api._mux_pairs(r)] == [540]
    assert api._mux_pairs({**_split_result(), "source": "ytapi"}) == []
    assert api._mux_pairs({**_split_result(), "gallery": [{"url": "x"}]}) == []


def test_v1_lists_merged_assets_but_legacy_output_is_untouched(with_ffmpeg):
    result = _split_result()
    before = json.dumps(result, sort_keys=True)
    record = api.make_v1_extraction("https://www.instagram.com/reel/abc/", result, "owner")
    merged = [a for a in record["assets"].values() if str(a["format"]).startswith("mux:")]
    assert len(merged) == 2 and all(a["has_audio"] is True and a["kind"] == "video" for a in merged)
    assert json.dumps(result, sort_keys=True) == before  # legacy /api/extract consumers see no change
    assert all("_video" not in a and "url" not in a for a in record["assets"].values())


def test_without_ffmpeg_v1_assets_are_exactly_what_they_were(monkeypatch):
    monkeypatch.setenv("FFMPEG_PATH", "/nonexistent/ffmpeg")
    monkeypatch.setattr(api, "_FFMPEG", "")
    record = api.make_v1_extraction("https://www.instagram.com/reel/abc/", _split_result(), "owner")
    assert not [a for a in record["assets"].values() if str(a["format"]).startswith("mux:")]
    assert record["assets"]["best"]["format"] == "best"


def test_real_mux_download_streams_one_file_with_both_tracks(monkeypatch, with_ffmpeg, tmp_path):
    import http.server, subprocess, threading

    def mk(args, name):
        subprocess.run([FFMPEG_FOR_TESTS, "-hide_banner", "-loglevel", "error", "-y", *args, str(tmp_path / name)], check=True)
    mk(["-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=25", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof"], "v.mp4")
    mk(["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "aac"], "a.m4a")

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            data = (tmp_path / self.path.lstrip("/")).read_bytes()
            m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
            if m:
                start = int(m.group(1)); end = int(m.group(2) or len(data) - 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                data = data[start:end + 1]
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        res = _split_result(base)
        res["formats"]["video"] = [r for r in res["formats"]["video"] if r["format_id"] == "dash-v1080"]
        res["formats"]["video"][0]["url"] = f"{base}/v.mp4"
        res["formats"]["audio"][0]["url"] = f"{base}/a.m4a"
        monkeypatch.setattr(api, "extract_v1_sync", lambda url, audio_only=False: res)
        token = c.post("/api/v1/client-sessions", json={"installation_id": "device-identifier-1234"}).json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        payload = c.post("/api/v1/extractions", json={"url": "https://www.instagram.com/reel/abc123/"}, headers=auth).json()
        asset = next(a for a in payload["assets"] if a["id"].startswith("m_"))
        r = c.get(asset["download_path"], headers=auth)
        assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
        out = tmp_path / "out.mp4"
        out.write_bytes(r.content)
        probe = subprocess.run([FFMPEG_FOR_TESTS, "-hide_banner", "-i", str(out), "-f", "null", "-"],
                               capture_output=True, text=True).stderr
        assert re.search(r"Stream #0:\d.*Video:", probe) and re.search(r"Stream #0:\d.*Audio:", probe)
        assert re.search(r"time=00:00:0[12]", probe)
    finally:
        srv.shutdown()


def test_mux_download_reports_a_vanished_quality_cleanly(monkeypatch, with_ffmpeg):
    monkeypatch.setattr(api, "extract_v1_sync", lambda url, audio_only=False: _split_result())
    token = c.post("/api/v1/client-sessions", json={"installation_id": "device-identifier-1234"}).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    payload = c.post("/api/v1/extractions", json={"url": "https://www.instagram.com/reel/abc123/"}, headers=auth).json()
    asset = next(a for a in payload["assets"] if a["id"].startswith("m_"))
    gone = _split_result()
    gone["formats"]["video"] = []
    monkeypatch.setattr(api, "extract_v1_sync", lambda url, audio_only=False: gone)
    api._cache.clear()  # force a fresh resolve so the vanished quality is observed
    r = c.get(asset["download_path"], headers=auth)
    assert r.status_code == 404 and "no longer listed" in r.json()["detail"]


def test_the_whole_candidate_chain_reaches_the_client(monkeypatch):
    chain = "; ".join(f"CAND{i}: LOGIN_REQUIRED (bot wall)" for i in range(8)) + "; MWEB+pot(visitor): no pot (BG client failed: boom)"
    monkeypatch.setattr(api, "YT_EXTRACT_URL", "https://ytapi.example")
    monkeypatch.setattr(api, "_ytapi_fetch", lambda url: (_ for _ in ()).throw(
        RuntimeError(f"extraction service: failed: No client returned playable streams [{chain}]")))
    monkeypatch.setattr(api, "run_ydl", lambda opts, url: _no_formats(url))
    token = c.post("/api/v1/client-sessions", json={"installation_id": "device-identifier-1234"}).json()["access_token"]
    r = c.post("/api/v1/extractions", json={"url": YT_URL}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 422
    assert "BG client failed: boom" in r.json()["detail"]  # the mint stage must not be cut off
