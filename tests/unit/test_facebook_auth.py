"""Tests for Facebook authenticated-session establishment via cookie import.

classify_auth_state and parse_cookie_export are pure and exhaustively tested.
ensure_logged_in is tested with a mocked page + injected load_cookies so we
assert: existing session skips import, checkpoints are reported (never
solved), missing cookies => NO_SESSION, and import success/failure classify.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from poob.sites.facebook import auth as fb_auth
from poob.sites.facebook.auth import (
    AuthStatus,
    _detect_state,
    classify_auth_state,
    ensure_logged_in,
    load_cookie_file,
    parse_cookie_export,
)


# --- classify_auth_state (pure) ---


class TestClassifyAuthState:
    def test_checkpoint_wins_over_loggedin(self):
        assert classify_auth_state(
            {"checkpoint": True, "loggedIn": True}
        ) is AuthStatus.CHECKPOINT

    def test_logged_in(self):
        assert classify_auth_state({"loggedIn": True}) is AuthStatus.LOGGED_IN

    def test_login_form_inconclusive(self):
        assert classify_auth_state({"hasLoginForm": True}) is None

    def test_non_dict(self):
        assert classify_auth_state(None) is None
        assert classify_auth_state("nope") is None


# --- parse_cookie_export (pure) ---


class TestParseCookieExport:
    def test_cookie_editor_list(self):
        raw = [
            {
                "name": "c_user", "value": "100", "domain": ".facebook.com",
                "path": "/", "secure": True, "httpOnly": True,
                "expirationDate": 1900000000, "sameSite": "no_restriction",
            },
            {"name": "xs", "value": "abc", "domain": ".facebook.com"},
        ]
        out = parse_cookie_export(raw)
        assert {c["name"] for c in out} == {"c_user", "xs"}
        c_user = next(c for c in out if c["name"] == "c_user")
        assert c_user["value"] == "100"
        assert c_user["domain"] == ".facebook.com"
        assert c_user["secure"] is True
        assert c_user["httpOnly"] is True
        assert c_user["expires"] == 1900000000.0
        assert c_user["sameSite"] == "None"  # no_restriction -> None

    def test_drops_non_facebook_cookies(self):
        out = parse_cookie_export([
            {"name": "g", "value": "1", "domain": ".google.com"},
            {"name": "c_user", "value": "1", "domain": ".facebook.com"},
        ])
        assert [c["name"] for c in out] == ["c_user"]

    def test_drops_cookies_without_name_or_value(self):
        out = parse_cookie_export([
            {"value": "x", "domain": ".facebook.com"},   # no name
            {"name": "y", "domain": ".facebook.com"},     # no value
            {"name": "ok", "value": "v", "domain": ".facebook.com"},
        ])
        assert [c["name"] for c in out] == ["ok"]

    def test_playwright_storage_state(self):
        raw = {"cookies": [
            {"name": "c_user", "value": "5", "domain": ".facebook.com", "expires": 123},
        ], "origins": []}
        out = parse_cookie_export(raw)
        assert out and out[0]["name"] == "c_user" and out[0]["expires"] == 123.0

    def test_json_string(self):
        raw = json.dumps([{"name": "xs", "value": "v", "domain": ".facebook.com"}])
        out = parse_cookie_export(raw)
        assert out[0]["name"] == "xs"

    def test_garbage(self):
        assert parse_cookie_export("not json") == []
        assert parse_cookie_export(None) == []
        assert parse_cookie_export(42) == []

    def test_defaults(self):
        out = parse_cookie_export([{"name": "n", "value": "v", "domain": "facebook.com"}])
        assert out[0]["path"] == "/"
        assert out[0]["secure"] is True
        assert out[0]["httpOnly"] is False
        assert "expires" not in out[0]  # no expiry given => session cookie


# --- load_cookie_file ---


class TestLoadCookieFile:
    def test_missing_file(self, tmp_path):
        assert load_cookie_file(tmp_path / "nope.json") == []

    def test_valid_file(self, tmp_path):
        p = tmp_path / "cookies.json"
        p.write_text(json.dumps([
            {"name": "c_user", "value": "9", "domain": ".facebook.com"},
        ]), encoding="utf-8")
        out = load_cookie_file(p)
        assert out and out[0]["name"] == "c_user"


# --- ensure_logged_in orchestration ---


def _make_page(detect_signals: list[dict]):
    page = AsyncMock()
    state = {"i": 0}

    async def _eval(js, *a, **k):
        idx = min(state["i"], len(detect_signals) - 1)
        state["i"] += 1
        return json.dumps(detect_signals[idx])

    page.evaluate = AsyncMock(side_effect=_eval)
    return page


@pytest.fixture(autouse=True)
def _no_nav(monkeypatch):
    monkeypatch.setattr(
        "poob.sites.facebook.auth.navigate_and_wait", AsyncMock(),
    )


def _cookie_file(tmp_path):
    p = tmp_path / "cookies.json"
    p.write_text(json.dumps([
        {"name": "c_user", "value": "1", "domain": ".facebook.com"},
        {"name": "xs", "value": "2", "domain": ".facebook.com"},
    ]), encoding="utf-8")
    return p


class TestEnsureLoggedIn:
    @pytest.mark.asyncio
    async def test_existing_session_skips_import(self, tmp_path):
        page = _make_page([{"loggedIn": True}])
        load = AsyncMock(return_value=2)
        status = await ensure_logged_in(
            page, cookies_path=_cookie_file(tmp_path), load_cookies=load,
        )
        assert status is AuthStatus.LOGGED_IN
        load.assert_not_awaited()  # already logged in -> no cookie import

    @pytest.mark.asyncio
    async def test_checkpoint_on_home(self, tmp_path):
        page = _make_page([{"checkpoint": True}])
        load = AsyncMock(return_value=2)
        status = await ensure_logged_in(
            page, cookies_path=_cookie_file(tmp_path), load_cookies=load,
        )
        assert status is AuthStatus.CHECKPOINT
        load.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_cookie_file(self, tmp_path):
        page = _make_page([{"hasLoginForm": True}])
        status = await ensure_logged_in(
            page, cookies_path=tmp_path / "absent.json",
            load_cookies=AsyncMock(return_value=0),
        )
        assert status is AuthStatus.NO_SESSION

    @pytest.mark.asyncio
    async def test_cookie_import_succeeds(self, tmp_path):
        # home: not logged in -> import cookies -> post: logged in
        page = _make_page([{"hasLoginForm": True}, {"loggedIn": True}])
        load = AsyncMock(return_value=2)
        status = await ensure_logged_in(
            page, cookies_path=_cookie_file(tmp_path), load_cookies=load,
        )
        assert status is AuthStatus.LOGGED_IN
        load.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cookie_import_stale(self, tmp_path):
        # cookies loaded but still not logged in -> FAILED
        page = _make_page([{"hasLoginForm": True}, {"hasLoginForm": True}])
        status = await ensure_logged_in(
            page, cookies_path=_cookie_file(tmp_path),
            load_cookies=AsyncMock(return_value=2),
        )
        assert status is AuthStatus.FAILED

    @pytest.mark.asyncio
    async def test_no_load_callable(self, tmp_path):
        page = _make_page([{"hasLoginForm": True}])
        status = await ensure_logged_in(
            page, cookies_path=_cookie_file(tmp_path), load_cookies=None,
        )
        assert status is AuthStatus.NO_SESSION

    @pytest.mark.asyncio
    async def test_import_succeeds_after_slow_hydration(self, tmp_path, monkeypatch):
        # home shows a login form (fast negative) -> import -> the post-cookie
        # page hydrates slowly: two inconclusive frames (no form, no chrome)
        # then the logged-in chrome. The boot-render race regressed exactly
        # this: the old fixed-delay probe declared FAILED on the blank frame.
        monkeypatch.setattr(fb_auth.asyncio, "sleep", AsyncMock())
        page = _make_page([
            {"hasLoginForm": True},                                   # home
            {"hasLoginForm": False, "hasAppChrome": False, "loggedIn": False},  # post t0
            {"hasLoginForm": False, "hasAppChrome": False, "loggedIn": False},  # post t1
            {"loggedIn": True, "hasAppChrome": True},                 # post t2
        ])
        status = await ensure_logged_in(
            page, cookies_path=_cookie_file(tmp_path),
            load_cookies=AsyncMock(return_value=6),
        )
        assert status is AuthStatus.LOGGED_IN


# --- _detect_state polling (the boot-render-race fix) ---


class TestDetectStatePolling:
    @pytest.mark.asyncio
    async def test_polls_through_hydration_until_chrome(self, monkeypatch):
        monkeypatch.setattr(fb_auth.asyncio, "sleep", AsyncMock())
        page = _make_page([
            {"hasLoginForm": False, "hasAppChrome": False, "loggedIn": False},
            {"hasLoginForm": False, "hasAppChrome": False, "loggedIn": False},
            {"loggedIn": True, "hasAppChrome": True},
        ])
        state = await _detect_state(page, label="post_cookies", settle_timeout_s=10)
        assert state is AuthStatus.LOGGED_IN
        assert page.evaluate.await_count == 3  # waited out hydration, didn't false-negative

    @pytest.mark.asyncio
    async def test_login_form_short_circuits_negative(self, monkeypatch):
        sleep = AsyncMock()
        monkeypatch.setattr(fb_auth.asyncio, "sleep", sleep)
        page = _make_page([{"hasLoginForm": True}])
        state = await _detect_state(page, label="home", settle_timeout_s=30)
        assert state is None
        assert page.evaluate.await_count == 1  # a visible login form ends the poll at once
        sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_checkpoint_short_circuits(self, monkeypatch):
        monkeypatch.setattr(fb_auth.asyncio, "sleep", AsyncMock())
        page = _make_page([{"checkpoint": True}])
        state = await _detect_state(page, label="home", settle_timeout_s=30)
        assert state is AuthStatus.CHECKPOINT
        assert page.evaluate.await_count == 1

    @pytest.mark.asyncio
    async def test_inconclusive_times_out_to_none(self, monkeypatch):
        monkeypatch.setattr(fb_auth.asyncio, "sleep", AsyncMock())
        page = _make_page([{"hasLoginForm": False, "hasAppChrome": False, "loggedIn": False}])
        state = await _detect_state(page, label="post_cookies", settle_timeout_s=3)
        assert state is None  # never conclusive -> times out negative
        assert page.evaluate.await_count == 3  # 3s / 1s poll
