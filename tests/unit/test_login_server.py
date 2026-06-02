"""Tests for the one-time remote-login session detection.

The infra orchestration (Xvfb/x11vnc/noVNC) isn't unit-testable, but the
"is the operator logged in yet?" check is, and it gates the cookie capture.
"""

from __future__ import annotations

from poob.login_server import _has_fb_session


def test_detects_xs_session():
    assert _has_fb_session([{"name": "xs", "value": "abc", "domain": ".facebook.com"}]) is True


def test_no_session_without_xs():
    assert _has_fb_session([{"name": "datr", "value": "x", "domain": ".facebook.com"}]) is False


def test_ignores_non_facebook_xs():
    assert _has_fb_session([{"name": "xs", "value": "x", "domain": ".google.com"}]) is False


def test_empty_cookies():
    assert _has_fb_session([]) is False


def test_xs_present_but_empty_value():
    assert _has_fb_session([{"name": "xs", "value": "", "domain": ".facebook.com"}]) is False
