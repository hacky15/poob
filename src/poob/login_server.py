"""One-time interactive Facebook login over a remote browser (noVNC).

The "perfection" path: instead of exporting/pasting cookie files (not scalable),
the operator logs into Facebook ONCE through a screen, and the rolled session is
captured to the volume's cookies.json — which the patrol imports and Fix B keeps
alive indefinitely (the repeated re-seeds were the wedge bug, now fixed). No
automated login (ToS/account-safety): this is a real human logging in once.

How it works
------------
Starts a virtual display (Xvfb) + a VNC bridge (x11vnc) + a web VNC client
(noVNC via websockify), then opens a HEADFUL Chromium on that display pointed at
Facebook. The operator reaches the screen over an SSH/Tailscale port-forward,
logs in (including any 2FA/checkpoint — it's their interactive session), and the
moment a session cookie (``xs``) appears, the cookies are written to the patrol's
``cookies.json`` and this process exits.

Run (typically a one-shot container sharing the patrol's browser volume):

    docker run --rm -p 127.0.0.1:6080:6080 \
        -v poob_poob-browser:/app/browser_profiles \
        --env-file .env ghcr.io/hacky15/poob:latest \
        python -m poob.login_server

Then from your machine: ``ssh -L 6080:localhost:6080 <homelab>`` and open
``http://localhost:6080/vnc.html``. See docs/runbooks/facebook-remote-login.md.

Security: the VNC/noVNC ports bind to localhost only — reach them via the SSH
tunnel above, never an exposed port.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from poob.utils.logging import get_logger

log = get_logger("login_server")

_DISPLAY = ":99"
_SCREEN = "1440x900x24"
_VNC_PORT = 5900
# noVNC web port (localhost-bound; reach via SSH/Tailscale port-forward).
_NOVNC_PORT = int(os.environ.get("POOB_LOGIN_NOVNC_PORT", "6080"))
# Max time the operator has to complete the login before we give up.
_LOGIN_DEADLINE_S = float(os.environ.get("POOB_LOGIN_TIMEOUT_S", "1800"))
# Where novnc's web assets live in the image (set by the apt 'novnc' package).
_NOVNC_WEB = os.environ.get("POOB_NOVNC_WEB", "/usr/share/novnc")
# Inside the container websockify must bind ALL interfaces: Docker's host-side
# `-p 127.0.0.1:<port>:<port>` publish forwards to the container's eth0, not its
# loopback, so a 127.0.0.1 bind here is unreachable from the host. The
# localhost-only security boundary is the host publish, NOT this bind. Override
# with POOB_LOGIN_BIND=127.0.0.1 only when running directly on a host (no Docker).
# See docs/runbooks/facebook-remote-login.md.
_BIND = os.environ.get("POOB_LOGIN_BIND", "0.0.0.0")


def _spawn(cmd: list[str]) -> subprocess.Popen:
    log.info("login.spawn", cmd=" ".join(cmd))
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _start_display_stack() -> list[subprocess.Popen]:
    """Bring up Xvfb + x11vnc + noVNC (websockify). Returns the procs to reap."""
    procs: list[subprocess.Popen] = []
    procs.append(_spawn(["Xvfb", _DISPLAY, "-screen", "0", _SCREEN, "-nolisten", "tcp"]))
    time.sleep(2.0)
    os.environ["DISPLAY"] = _DISPLAY
    # A minimal window manager keeps focus/inputs sane during login (optional).
    try:
        procs.append(_spawn(["fluxbox"]))
    except FileNotFoundError:
        log.info("fluxbox not installed — continuing without a window manager")
    # VNC server for the virtual display, localhost-only.
    procs.append(_spawn([
        "x11vnc", "-display", _DISPLAY, "-nopw", "-localhost",
        "-forever", "-shared", "-rfbport", str(_VNC_PORT), "-quiet",
    ]))
    time.sleep(1.0)
    # noVNC web client. Binds _BIND (0.0.0.0 in Docker) so the host-localhost
    # publish can reach it; never expose the host port to 0.0.0.0 — reach it via
    # the SSH/Tailscale tunnel only.
    procs.append(_spawn([
        "websockify", "--web", _NOVNC_WEB,
        f"{_BIND}:{_NOVNC_PORT}", f"localhost:{_VNC_PORT}",
    ]))
    return procs


def _has_fb_session(cookies: list[dict]) -> bool:
    """True once a Facebook session cookie (``xs``) is present in the browser."""
    return any(
        isinstance(c, dict)
        and c.get("name") == "xs"
        and "facebook.com" in str(c.get("domain") or "")
        and c.get("value")
        for c in cookies
    )


async def _drive_login() -> int:
    from poob.browser.manager import BrowserManager
    from poob.config import AppConfig

    config = AppConfig()
    cookies_path = config.browser_profiles_dir / "facebook" / "cookies.json"
    # Dedicated profile dir so we never fight the patrol's profile lock.
    mgr = BrowserManager(headless=False, profiles_dir=config.browser_profiles_dir)
    await mgr.start(cookies_file="login/session.json")
    page = await mgr.get_page()
    try:
        await page.goto("https://www.facebook.com/")  # type: ignore[attr-defined]
    except Exception:
        # browser-use Page API varies; the operator can navigate manually in noVNC.
        log.info("Could not auto-navigate — navigate to facebook.com yourself in noVNC")

    log.info(
        "Remote login ready — open the screen and log into Facebook",
        connect=(
            f"ssh -L {_NOVNC_PORT}:localhost:{_NOVNC_PORT} <homelab>  then  "
            f"http://localhost:{_NOVNC_PORT}/vnc.html"
        ),
        deadline_min=round(_LOGIN_DEADLINE_S / 60),
    )

    deadline = time.monotonic() + _LOGIN_DEADLINE_S
    while time.monotonic() < deadline:
        try:
            cookies = await mgr._read_all_cookies()
        except Exception as exc:
            log.debug("cookie read failed (browser still starting?)", error=str(exc)[:80])
            cookies = []
        if _has_fb_session(cookies):
            n = await mgr.persist_cookies(cookies_path)
            if n > 0:
                log.info(
                    "Captured Facebook session — patrol will adopt it on next "
                    "cycle and Fix B keeps it alive. You're done; you can stop here.",
                    count=n,
                    path=str(cookies_path),
                )
                await mgr.stop()
                return 0
        await asyncio.sleep(5)

    log.error("Remote login timed out — no Facebook session captured", deadline_s=_LOGIN_DEADLINE_S)
    await mgr.stop()
    return 1


def main() -> None:
    procs = _start_display_stack()
    rc = 1
    try:
        rc = asyncio.run(_drive_login())
    except KeyboardInterrupt:
        rc = 130
    finally:
        for p in reversed(procs):
            try:
                p.terminate()
            except Exception:
                pass
    sys.exit(rc)


if __name__ == "__main__":
    main()
