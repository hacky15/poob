"""Diagnostic: load FB with the captured cookies and watch auth signals over time.

Answers one question: does the logged-in app chrome appear if we wait longer
than ensure_logged_in's fixed 2500ms? If hasAppChrome flips True after a few
seconds, the boot auth check is false-negativing on a timing race (CPU-slow
homelab + heavy FB SPA), not on a bad session. Run as a one-shot container
sharing the patrol's browser volume. Read-only w.r.t. cookies.json.
"""

from __future__ import annotations

import asyncio

from poob.browser.manager import BrowserManager
from poob.browser.page_actions import navigate_and_wait
from poob.config import AppConfig
from poob.sites.facebook.auth import _DETECT_STATE_JS, load_cookie_file
from poob.utils.content import parse_evaluate_result


async def main() -> None:
    config = AppConfig()
    cookies_path = config.browser_profiles_dir / "facebook" / "cookies.json"
    cookies = load_cookie_file(cookies_path)
    print(f"cookies parsed: {len(cookies)} names={[c['name'] for c in cookies]}", flush=True)

    mgr = BrowserManager(headless=True, profiles_dir=config.browser_profiles_dir)
    await mgr.start(cookies_file="diag/session.json")
    page = await mgr.get_page()
    await navigate_and_wait(page, "https://www.facebook.com/", wait_ms=1500)
    n = await mgr.load_cookies(cookies)
    print(f"cookies loaded into session: {n}", flush=True)
    await navigate_and_wait(page, "https://www.facebook.com/", wait_ms=1500)

    for i in range(15):
        try:
            sig = parse_evaluate_result(await page.evaluate(_DETECT_STATE_JS)) or {}
            print(
                f"t={i:2d}s url={str(sig.get('url'))[:55]:55} "
                f"loginForm={sig.get('hasLoginForm')} checkpoint={sig.get('checkpoint')} "
                f"appChrome={sig.get('hasAppChrome')} loggedIn={sig.get('loggedIn')}",
                flush=True,
            )
            if sig.get("hasAppChrome"):
                print(">>> app chrome appeared — session IS valid; boot check was too early", flush=True)
                break
        except Exception as exc:  # noqa: BLE001
            print(f"t={i}s eval error: {str(exc)[:90]}", flush=True)
        await asyncio.sleep(1)

    try:
        info = parse_evaluate_result(
            await page.evaluate(
                "() => ({title: document.title, "
                "marketplace: !!document.querySelector('a[href*=\"/marketplace\"]'), "
                "banner: !!document.querySelector('[role=\"banner\"]'), "
                "nav: !!document.querySelector('div[role=\"navigation\"]'), "
                "body: document.body ? document.body.innerText.slice(0,200) : ''})"
            )
        ) or {}
        print(f"TITLE: {str(info.get('title'))[:90]}", flush=True)
        print(f"selectors: marketplace={info.get('marketplace')} banner={info.get('banner')} nav={info.get('nav')}", flush=True)
        print(f"BODY: {str(info.get('body'))[:200].replace(chr(10), ' ')}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"info error: {str(exc)[:90]}", flush=True)

    await mgr.stop()


if __name__ == "__main__":
    asyncio.run(main())
