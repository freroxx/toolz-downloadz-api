"""Cookie lifecycle tests — analysis, gating, admin endpoints."""
import os
import time

os.environ.setdefault("API_SECRET_KEY", "test123")

import pytest
from fastapi.testclient import TestClient
from api.index import app, _analyze_cookies, get_youtube_cookies, _cookie_memo

client = TestClient(app)
H = {"X-API-KEY": "test123"}


def _mk(sidcc_days=10, with_login=True):
    exp = int(time.time() + sidcc_days * 86400)
    lines = ["# Netscape HTTP Cookie File"]
    if with_login:
        lines += [
            f".youtube.com\tTRUE\t/\tTRUE\t{exp}\tSID\tsomesid",
            f".youtube.com\tTRUE\t/\tTRUE\t{exp}\t__Secure-1PSID\tsomepsid",
            f".youtube.com\tTRUE\t/\tTRUE\t{exp}\tLOGIN_INFO\tAFmmF2sw",
        ]
    lines.append(f".youtube.com\tTRUE\t/\tTRUE\t{exp}\tSIDCC\tAKEyXz")
    lines.append(f".youtube.com\tTRUE\t/\tTRUE\t{exp}\t__Secure-1PSIDCC\tAKEyXz")
    return "\n".join(lines) + "\n"


def test_analyze_fresh():
    a = _analyze_cookies(_mk(10))
    assert a["verdict"] == "fresh"
    assert a["logged_in"] is True
    assert 9 < a["days_left"] <= 10.1


def test_analyze_expired():
    a = _analyze_cookies(_mk(-3))
    assert a["verdict"] == "expired"
    assert len(a["expired"]) >= 2


def test_analyze_aging():
    a = _analyze_cookies(_mk(1))
    assert a["verdict"] in ("aging", "expired")  # 1d boundary
    if a["verdict"] == "aging":
        assert a["days_left"] is not None


def test_analyze_not_logged_in():
    a = _analyze_cookies("a.b.c\tTRUE\t/\tTRUE\t9999999999\tPREF\tx\n")
    assert a["verdict"] == "not_logged_in"


def test_analyze_invalid():
    assert _analyze_cookies("") ["verdict"] == "none_set"
    assert _analyze_cookies("hello world not cookies") ["verdict"] == "invalid"


def test_gating_expired_sends_nothing(monkeypatch):
    monkeypatch.setattr("api.index.YOUTUBE_COOKIES", _mk(-5))
    monkeypatch.setattr("api.index.KV_REST_URL", "")
    _cookie_memo.update({"ts": 0, "payload": None})
    ck = get_youtube_cookies()
    assert ck["content"] is None
    assert ck["verdict"] == "expired"


def test_gating_fresh_sends(monkeypatch):
    monkeypatch.setattr("api.index.YOUTUBE_COOKIES", _mk(10))
    monkeypatch.setattr("api.index.KV_REST_URL", "")
    _cookie_memo.update({"ts": 0, "payload": None})
    ck = get_youtube_cookies()
    assert ck["content"] and ck["verdict"] == "fresh"


def test_diag_requires_auth():
    assert client.get("/api/diag").status_code == 401


def test_admin_get_reports_verdict(monkeypatch):
    monkeypatch.setattr("api.index.YOUTUBE_COOKIES", _mk(10))
    monkeypatch.setattr("api.index.KV_REST_URL", "")
    _cookie_memo.update({"ts": 0, "payload": None})
    r = client.get("/api/admin/cookies", headers=H)
    assert r.status_code == 200
    j = r.json()
    assert j["verdict"] == "fresh"
    assert "content" not in j  # never echo values


def test_admin_post_rejects_invalid(monkeypatch):
    monkeypatch.setattr("api.index.KV_REST_URL", "https://example.upstash.io")
    monkeypatch.setattr("api.index.KV_REST_TOKEN", "t")
    r = client.post("/api/admin/cookies", content="garbage",
                    headers={**H, "Content-Type": "text/plain"})
    assert r.status_code == 400
    assert "Rejected" in r.json()["detail"]


def test_admin_post_without_kv_returns_501(monkeypatch):
    monkeypatch.setattr("api.index.KV_REST_URL", "")
    r = client.post("/api/admin/cookies", content=_mk(10),
                    headers={**H, "Content-Type": "text/plain"})
    assert r.status_code == 501


def test_blocked_card_carries_cookie_verdict(monkeypatch):
    """Expired cookies → blocked message names them explicitly."""
    from api.index import extract_sync
    monkeypatch.setattr("api.index.YOUTUBE_COOKIES", _mk(-5))
    _cookie_memo.update({"ts": 0, "payload": None})
    try:
        r = extract_sync("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    except RuntimeError:
        pytest.skip("network unavailable")
    if not r.get("blocked"):
        pytest.skip("IP not bot-flagged — blocked card path untestable here")
    msg = r.get("blocked_message", "") or ""
    assert "cookies=" in msg and ("expired" in msg or "aging" in msg)


def test_potcheck_auth():
    assert client.get("/api/potcheck").status_code == 401
