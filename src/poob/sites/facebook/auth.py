"""Facebook authenticated-session establishment for the patrol browser.

Goal: a logged-in FB session, headless, with NO ongoing human intervention,
established by reusing a persisted cookie session when present and otherwise
performing a one-time headless login with the operator's .env credentials.

Hard constraints (operator standing rules + project legality posture):
  - NEVER attempt to solve a CAPTCHA / checkpoint programmatically. That is
    anti-bot bypass, which is forbidden. On a checkpoint we STOP and report
    CHECKPOINT so the caller falls back to the anonymous path.
  - The login event is the #1 CAPTCHA trigger, so we only log in when there
    is no valid session (cookies persist in the browser_profiles volume, so
    a successful login is reused for weeks). The residential outbound IP
    keeps checkpoint risk low but cannot eliminate it.

See docs/decisions/authenticated-session-no-human.md.
"""

from __future__ import annotations

import json
from enum import Enum

from poob.browser.page_actions import navigate_and_wait
from poob.utils.content import parse_evaluate_result
from poob.utils.logging import get_logger

log = get_logger("sites.facebook.auth")

_FB_HOME = "https://www.facebook.com/"
_FB_LOGIN = "https://www.facebook.com/login/"


class AuthStatus(str, Enum):
    """Outcome of an authentication attempt."""

    LOGGED_IN = "logged_in"          # Valid authenticated session present
    CHECKPOINT = "checkpoint"        # FB demands a CAPTCHA/2FA — we do NOT solve it
    FAILED = "failed"                # Login submitted but session not established
    NO_CREDENTIALS = "no_credentials"  # No email/password configured


# Detect the current auth state of whatever FB page is loaded. Returns a
# signals dict (parsed from the JSON-stringified evaluate result).
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
    // Logged-in chrome: marketplace link, left nav, or profile banner present
    // and NO login form.
    const hasAppChrome = !!document.querySelector(
        'a[href*="/marketplace"], div[role="navigation"], [aria-label="Your profile"], [role="banner"]'
    );
    const loggedIn = !hasLoginForm && !checkpoint && hasAppChrome;
    return { url, hasLoginForm, checkpoint, hasAppChrome, loggedIn };
}"""


def classify_auth_state(signals: dict) -> AuthStatus | None:
    """Classify page signals into an auth state.

    Pure function (no I/O) so the decision logic is unit-testable.

    Args:
        signals: dict from ``_DETECT_STATE_JS`` with keys ``loggedIn``,
            ``checkpoint``, ``hasLoginForm``.

    Returns:
        ``AuthStatus.LOGGED_IN`` / ``CHECKPOINT`` when determinable, or
        ``None`` when the page is the login form (caller should attempt
        login) or the state is otherwise inconclusive.
    """
    if not isinstance(signals, dict):
        return None
    if signals.get("checkpoint"):
        return AuthStatus.CHECKPOINT
    if signals.get("loggedIn"):
        return AuthStatus.LOGGED_IN
    return None  # login form present or inconclusive → caller decides


async def _detect_state(page: object) -> AuthStatus | None:
    """Load nothing; read the current page's auth signals."""
    try:
        raw = await page.evaluate(_DETECT_STATE_JS)
        signals = parse_evaluate_result(raw) or {}
        return classify_auth_state(signals)
    except Exception as exc:
        log.debug("auth state detection failed", error=str(exc)[:100])
        return None


def _build_login_js(email: str, password: str) -> str:
    """Build the form-fill+submit JS with creds JSON-embedded.

    The returned string contains the password — it MUST NOT be logged.
    """
    creds = json.dumps({"email": email, "password": password})
    # MUST be a bare arrow function (like every other JS in this codebase),
    # NOT a self-invoking IIFE — browser-use's page.evaluate CALLS the
    # function, so an IIFE throws/misbehaves.
    return (
        "() => {"
        f"  const c = {creds};"
        "  const email = document.querySelector('input[name=\"email\"], input#email');"
        "  const pass = document.querySelector('input[name=\"pass\"], input#pass');"
        "  if (!email || !pass) return JSON.stringify({ok:false, reason:'no_form'});"
        "  const set = (el, val) => {"
        "    const d = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');"
        "    (d && d.set ? d.set : (v)=>{el.value=v;}).call(el, val);"
        "    el.dispatchEvent(new Event('input', {bubbles:true}));"
        "    el.dispatchEvent(new Event('change', {bubbles:true}));"
        "  };"
        "  set(email, c.email); set(pass, c.password);"
        "  const btn = document.querySelector("
        "    'button[name=\"login\"], [data-testid=\"royal_login_button\"], button[type=\"submit\"]');"
        "  if (btn) { btn.click(); return JSON.stringify({ok:true, method:'click'}); }"
        "  if (pass.form) { pass.form.submit(); return JSON.stringify({ok:true, method:'form'}); }"
        "  return JSON.stringify({ok:false, reason:'no_submit'});"
        "}"
    )


async def ensure_logged_in(
    page: object,
    email: str,
    password: str,
    *,
    post_submit_wait_ms: int = 6000,
) -> AuthStatus:
    """Ensure the browser has a valid authenticated FB session.

    1. Load the FB home page; if already logged in (persisted cookies),
       return LOGGED_IN without a login event.
    2. If a checkpoint is showing, return CHECKPOINT (we never solve it).
    3. Otherwise, if credentials exist, perform a one-time headless login
       and re-detect the resulting state.

    Args:
        page: A browser-use Page from the (main) BrowserManager.
        email: FB account email.
        password: FB account password.
        post_submit_wait_ms: How long to wait for navigation after submit.

    Returns:
        The resulting AuthStatus.
    """
    import asyncio as _asyncio

    # Step 1: load home, check existing session (cookie reuse — no login event).
    await navigate_and_wait(page, _FB_HOME, wait_ms=2500)
    state = await _detect_state(page)
    if state is AuthStatus.LOGGED_IN:
        log.info("FB session valid — reusing persisted cookies (no login)")
        return AuthStatus.LOGGED_IN
    if state is AuthStatus.CHECKPOINT:
        log.warning(
            "FB checkpoint on home — not solving (anti-bot bypass forbidden); "
            "falling back to anonymous path",
        )
        return AuthStatus.CHECKPOINT

    if not email or not password:
        log.info("No FB credentials configured — staying anonymous")
        return AuthStatus.NO_CREDENTIALS

    # Step 2: one-time headless login.
    log.info("No valid FB session — attempting headless login")
    await navigate_and_wait(page, _FB_LOGIN, wait_ms=2500)
    try:
        await page.evaluate(_build_login_js(email, password))  # never log this JS
    except Exception as exc:
        log.warning("FB login form fill failed", error=str(exc)[:100])
        return AuthStatus.FAILED
    await _asyncio.sleep(post_submit_wait_ms / 1000.0)

    # Step 3: re-detect outcome.
    state = await _detect_state(page)
    if state is AuthStatus.LOGGED_IN:
        log.info("FB headless login succeeded — session established")
        return AuthStatus.LOGGED_IN
    if state is AuthStatus.CHECKPOINT:
        log.warning(
            "FB checkpoint after login — not solving; falling back to anonymous. "
            "A one-time cookie refresh on a trusted machine may be needed.",
        )
        return AuthStatus.CHECKPOINT
    log.warning("FB login did not establish a session (no checkpoint detected)")
    return AuthStatus.FAILED
