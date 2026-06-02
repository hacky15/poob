"""Facebook authenticated-session establishment for the patrol browser.

Strategy (operator decision 2026-05-29): a **one-time cookie import**. FB
tolerates a headless login from the residential IP (no CAPTCHA observed) but
hardens the credential *submit* against automation, so automating the login
is brittle and FB-hostile. Instead the operator exports their real FB session
cookies once (a ~2-minute local browser task) and the patrol browser adopts
them. The cookies persist in the `browser_profiles` Docker volume, so the
import lasts weeks and there is no ongoing human intervention.

Hard constraints (operator standing rules + project legality posture):
  - NEVER solve a CAPTCHA / checkpoint programmatically (anti-bot bypass).
    On a checkpoint we STOP, log, and fall back to the anonymous path.
  - We do NOT automate credential login (it doesn't reliably work and
    repeatedly POSTing creds risks flagging the account). Cookie import only.

See docs/decisions/authenticated-session-no-human.md and
docs/runbooks/facebook-cookie-import.md.
"""

from __future__ import annotations

import asyncio
import json
import os
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from poob.browser.page_actions import navigate_and_wait
from poob.utils.content import parse_evaluate_result
from poob.utils.logging import get_logger

log = get_logger("sites.facebook.auth")

_FB_HOME = "https://www.facebook.com/"

# FB home is an async-hydrated SPA. The logged-in chrome can take many seconds
# to render — especially at boot, when the auth check races container startup
# (model loads) on a CPU-only host. A single fixed-delay probe false-negatives a
# valid session there, leaving the patrol anonymous until the next restart (see
# docs/incidents/fb-auth-boot-render-race.md). So we POLL the auth signals until
# a conclusive verdict instead of trusting one delayed snapshot. A login form is
# a conclusive negative (we stop); "neither form nor chrome" means still
# hydrating (we keep waiting). Tunable via env for slow/fast hosts.
_AUTH_SETTLE_HOME_S = float(os.environ.get("POOB_FB_AUTH_SETTLE_HOME_S", "12"))
_AUTH_SETTLE_POST_S = float(os.environ.get("POOB_FB_AUTH_SETTLE_POST_S", "25"))
_AUTH_POLL_S = float(os.environ.get("POOB_FB_AUTH_POLL_S", "1.0"))


class AuthStatus(str, Enum):
    """Outcome of an authentication attempt."""

    LOGGED_IN = "logged_in"      # Valid authenticated session present
    CHECKPOINT = "checkpoint"    # FB demands a CAPTCHA/2FA — we do NOT solve it
    NO_SESSION = "no_session"    # Not logged in and no usable cookies to import
    FAILED = "failed"            # Cookies imported but session still not valid


# Detect the current auth state of whatever FB page is loaded.
_DETECT_STATE_JS = """() => {
    const url = location.href;
    const hasEmail = !!document.querySelector('input[name="email"], input#email');
    const hasPass  = !!document.querySelector('input[name="pass"], input#pass');
    const hasLoginForm = hasEmail && hasPass;
    const bodyText = document.body ? document.body.innerText.slice(0, 4000) : '';
    const checkpoint =
        /\\/checkpoint\\/|two_step_verification|two_factor|confirmemail|recover|captcha/i.test(url)
        || !!document.querySelector('input[name="captcha_response"], iframe[title*="captcha" i], iframe[src*="captcha" i]')
        || /enter (the|your) (security |login )?code|confirm your identity|we need to confirm|unusual (login|activity)|verify it.s you/i.test(bodyText);
    const hasAppChrome = !!document.querySelector(
        'a[href*="/marketplace"], div[role="navigation"], [aria-label="Your profile"], [role="banner"]'
    );
    const loggedIn = !hasLoginForm && !checkpoint && hasAppChrome;
    return { url, hasLoginForm, checkpoint, hasAppChrome, loggedIn };
}"""

# Map common export sameSite spellings -> CDP values.
_SAMESITE = {
    "no_restriction": "None", "none": "None", "unspecified": "Lax",
    "lax": "Lax", "strict": "Strict",
}


def classify_auth_state(signals: dict) -> AuthStatus | None:
    """Classify page signals into an auth state (pure; unit-testable)."""
    if not isinstance(signals, dict):
        return None
    if signals.get("checkpoint"):
        return AuthStatus.CHECKPOINT
    if signals.get("loggedIn"):
        return AuthStatus.LOGGED_IN
    return None  # login form / inconclusive


def parse_cookie_export(raw: Any) -> list[dict]:
    """Normalize an exported cookie blob into CDP setCookies params.

    Accepts (all common export shapes):
      - a JSON string of either form below,
      - a list of cookie dicts (Cookie-Editor / EditThisCookie format),
      - a Playwright storage_state dict ``{"cookies": [...]}``.

    Returns a list of dicts with CDP keys: name, value, domain, path,
    secure, httpOnly, sameSite, and (when known) expires. Only
    facebook-domain cookies with a name+value survive. Pure function.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
    if isinstance(raw, dict):
        raw = raw.get("cookies", [])
    if not isinstance(raw, list):
        return []

    out: list[dict] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        domain = (c.get("domain") or ".facebook.com").strip()
        if "facebook.com" not in domain:
            continue  # only adopt FB cookies
        param: dict[str, Any] = {
            "name": str(name),
            "value": str(value),
            "domain": domain,
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", False)),
        }
        ss = c.get("sameSite")
        if isinstance(ss, str) and ss.lower() in _SAMESITE:
            param["sameSite"] = _SAMESITE[ss.lower()]
        exp = c.get("expirationDate", c.get("expires"))
        if isinstance(exp, (int, float)) and exp > 0:
            param["expires"] = float(exp)
        out.append(param)
    return out


def load_cookie_file(path: str | Path) -> list[dict]:
    """Read + parse a cookie export file; [] if missing/empty/invalid."""
    p = Path(path)
    if not p.is_file():
        return []
    try:
        return parse_cookie_export(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — never let a bad file crash startup
        log.warning("Failed to read FB cookie file", path=str(p), error=str(exc)[:100])
        return []


def _log_signals(label: str, signals: dict, *, settled_s: float, timed_out: bool = False) -> None:
    log.info(
        "FB auth signals",
        phase=label,
        settled_s=round(settled_s, 1),
        timed_out=timed_out or None,
        url=str(signals.get("url", ""))[:120],
        has_login_form=signals.get("hasLoginForm"),
        checkpoint=signals.get("checkpoint"),
        has_app_chrome=signals.get("hasAppChrome"),
        logged_in=signals.get("loggedIn"),
    )


async def _detect_state(
    page: object, *, label: str = "", settle_timeout_s: float = _AUTH_SETTLE_POST_S
) -> AuthStatus | None:
    """Poll the page's auth signals until a conclusive verdict or timeout.

    Short-circuits the instant the page is conclusive: LOGGED_IN / CHECKPOINT
    (positive) or a visible login form (negative). "Neither form nor chrome"
    means the logged-in SPA is still hydrating, so we keep polling rather than
    declaring failure on a fixed delay — that fixed-delay snapshot was the
    boot-render race that left valid sessions unauthenticated. Logs URL+flags,
    never credentials.
    """
    iterations = max(1, int(settle_timeout_s / _AUTH_POLL_S))
    signals: dict = {}
    for i in range(iterations):
        try:
            signals = parse_evaluate_result(await page.evaluate(_DETECT_STATE_JS)) or {}
        except Exception as exc:
            log.debug("auth state detection failed", error=str(exc)[:100])
            await asyncio.sleep(_AUTH_POLL_S)
            continue
        state = classify_auth_state(signals)
        if state is not None:  # LOGGED_IN or CHECKPOINT — conclusive positive
            _log_signals(label, signals, settled_s=i * _AUTH_POLL_S)
            return state
        if signals.get("hasLoginForm"):  # conclusive negative — chrome isn't coming
            _log_signals(label, signals, settled_s=i * _AUTH_POLL_S)
            return None
        await asyncio.sleep(_AUTH_POLL_S)  # inconclusive — still hydrating, re-poll
    _log_signals(label, signals, settled_s=settle_timeout_s, timed_out=True)
    return classify_auth_state(signals)


async def ensure_logged_in(
    page: object,
    *,
    cookies_path: str | Path,
    load_cookies: Callable[[list[dict]], Awaitable[int]] | None = None,
) -> AuthStatus:
    """Ensure the browser has a valid authenticated FB session via cookies.

    1. Load FB home; if already logged in (cookies persisted in the volume),
       return LOGGED_IN.
    2. If a checkpoint is showing, return CHECKPOINT (never solved).
    3. Otherwise import cookies from ``cookies_path`` (operator's one-time
       export), reload, and re-check. LOGGED_IN on success; FAILED if the
       cookies are expired/invalid; NO_SESSION if there's no cookie file.

    Args:
        page: A browser-use Page from the (main) BrowserManager.
        cookies_path: Path to the exported FB cookie JSON.
        load_cookies: Async callable that injects cookies into the live
            session (BrowserManager.load_cookies). If None, cookie import
            is skipped (detection-only).

    Returns:
        The resulting AuthStatus.
    """
    await navigate_and_wait(page, _FB_HOME, wait_ms=800)
    state = await _detect_state(page, label="home", settle_timeout_s=_AUTH_SETTLE_HOME_S)
    if state is AuthStatus.LOGGED_IN:
        log.info("FB session valid — reusing persisted/imported cookies")
        return AuthStatus.LOGGED_IN
    if state is AuthStatus.CHECKPOINT:
        log.warning("FB checkpoint on home — not solving; staying anonymous")
        return AuthStatus.CHECKPOINT

    cookies = load_cookie_file(cookies_path)
    if not cookies or load_cookies is None:
        log.info(
            "No FB session and no importable cookies — staying anonymous. "
            "Import cookies per docs/runbooks/facebook-cookie-import.md",
            cookies_path=str(cookies_path),
            had_file=bool(cookies),
        )
        return AuthStatus.NO_SESSION

    try:
        count = await load_cookies(cookies)
        log.info("Imported FB cookies into session", count=count)
    except Exception as exc:
        log.warning("FB cookie import failed", error=str(exc)[:120])
        return AuthStatus.NO_SESSION

    await navigate_and_wait(page, _FB_HOME, wait_ms=800)
    state = await _detect_state(page, label="post_cookies", settle_timeout_s=_AUTH_SETTLE_POST_S)
    if state is AuthStatus.LOGGED_IN:
        log.info("FB session established from imported cookies")
        return AuthStatus.LOGGED_IN
    if state is AuthStatus.CHECKPOINT:
        log.warning("FB checkpoint after cookie import — not solving; staying anonymous")
        return AuthStatus.CHECKPOINT
    log.warning(
        "Imported FB cookies did not yield a logged-in session "
        "(expired/invalid?) — refresh per runbook; staying anonymous",
    )
    return AuthStatus.FAILED
