"""Tests for Facebook authenticated-session establishment.

The decision logic (classify_auth_state) is pure and exhaustively tested.
The ensure_logged_in orchestration is tested with a mocked page so we can
assert: cookie reuse skips login, checkpoints are reported (never solved),
and login outcomes classify correctly.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from poob.sites.facebook.auth import (
    AuthStatus,
    _build_login_js,
    classify_auth_state,
    ensure_logged_in,
)


# --- classify_auth_state (pure) ---


class TestClassifyAuthState:
    def test_checkpoint_wins_over_loggedin(self):
        # If FB shows a checkpoint we must NOT treat it as logged in.
        assert classify_auth_state(
            {"checkpoint": True, "loggedIn": True}
        ) is AuthStatus.CHECKPOINT

    def test_logged_in(self):
        assert classify_auth_state({"loggedIn": True}) is AuthStatus.LOGGED_IN

    def test_login_form_is_inconclusive(self):
        # Login form present → caller should attempt login → None.
        assert classify_auth_state({"hasLoginForm": True}) is None

    def test_empty_inconclusive(self):
        assert classify_auth_state({}) is None

    def test_non_dict(self):
        assert classify_auth_state(None) is None
        assert classify_auth_state("nope") is None


# --- _build_login_js ---


class TestBuildLoginJs:
    def test_embeds_credentials(self):
        js = _build_login_js("user@example.com", "hunter2")
        assert "user@example.com" in js
        assert "hunter2" in js

    def test_is_self_invoking_expression(self):
        js = _build_login_js("a@b.com", "x")
        assert js.strip().startswith("(()")
        assert "royal_login_button" in js  # targets FB's login button


# --- ensure_logged_in orchestration ---


def _make_page(detect_signals: list[dict], login_ok: bool = True):
    """Mock page whose evaluate() returns the given detect signals in order
    (for the detection JS) and a login-ok payload for the login JS."""
    page = AsyncMock()
    state = {"i": 0}

    async def _eval(js, *args, **kwargs):
        if "loggedIn" in js:  # detection JS
            idx = min(state["i"], len(detect_signals) - 1)
            state["i"] += 1
            return json.dumps(detect_signals[idx])
        return json.dumps({"ok": login_ok, "method": "click"})  # login JS

    page.evaluate = AsyncMock(side_effect=_eval)
    return page


@pytest.fixture(autouse=True)
def _no_nav_no_sleep():
    """Stub navigation + sleep so tests don't hit the network or wait 6s."""
    with patch(
        "poob.sites.facebook.auth.navigate_and_wait", new_callable=AsyncMock
    ), patch("asyncio.sleep", new_callable=AsyncMock):
        yield


class TestEnsureLoggedIn:
    @pytest.mark.asyncio
    async def test_reuses_existing_session_no_login(self):
        page = _make_page([{"loggedIn": True}])
        status = await ensure_logged_in(page, "a@b.com", "pw")
        assert status is AuthStatus.LOGGED_IN
        # Only the home detection ran — no login JS evaluated.
        assert page.evaluate.await_count == 1

    @pytest.mark.asyncio
    async def test_checkpoint_on_home_not_solved(self):
        page = _make_page([{"checkpoint": True}])
        status = await ensure_logged_in(page, "a@b.com", "pw")
        assert status is AuthStatus.CHECKPOINT
        assert page.evaluate.await_count == 1  # no login attempted

    @pytest.mark.asyncio
    async def test_no_credentials(self):
        page = _make_page([{"hasLoginForm": True}])  # not logged in
        status = await ensure_logged_in(page, "", "")
        assert status is AuthStatus.NO_CREDENTIALS

    @pytest.mark.asyncio
    async def test_login_succeeds(self):
        # home: not logged in -> login -> post-login: logged in
        page = _make_page([{"hasLoginForm": True}, {"loggedIn": True}])
        status = await ensure_logged_in(page, "a@b.com", "pw")
        assert status is AuthStatus.LOGGED_IN

    @pytest.mark.asyncio
    async def test_login_hits_checkpoint(self):
        page = _make_page([{"hasLoginForm": True}, {"checkpoint": True}])
        status = await ensure_logged_in(page, "a@b.com", "pw")
        assert status is AuthStatus.CHECKPOINT

    @pytest.mark.asyncio
    async def test_login_fails_no_session(self):
        page = _make_page([{"hasLoginForm": True}, {"hasLoginForm": True}])
        status = await ensure_logged_in(page, "a@b.com", "pw")
        assert status is AuthStatus.FAILED
