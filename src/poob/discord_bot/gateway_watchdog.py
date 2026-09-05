"""Backstop for a wedged Discord gateway that will never recover on its own.

2026-09-05 ~01:08 UTC production: a voice auto-join completed, then 60.15s
later py-cord's KeepAliveHandler thread logged "Shard ID None has stopped
responding to the gateway. Closing and restarting." — and nothing else,
ever, for 6+ minutes until a manual container restart. Confirmed live via
SSH: process alive, non-zero CPU, main thread idle in `epoll_wait`
(`/proc/1/task/1/wchan` == `ep_poll`), zero further log output of any kind.

Root cause traced into the installed py-cord dependency itself
(`discord/gateway.py`, `KeepAliveHandler.run()`): once staleness is
detected, its own recovery is

    coro = self.ws.close(4000)
    f = asyncio.run_coroutine_threadsafe(coro, loop=self.ws.loop)
    f.result()          # no timeout
    ...
    self.stop()          # never reached if the above hangs

If `ws.close(4000)` never completes — e.g. the transport is already a
zombie after a silent network drop — `f.result()` blocks the
KeepAliveHandler thread forever, so the thread that is supposed to let a
fresh reconnect happen just dies wedged. This is a real, unbounded-block
defect in the dependency, not something patchable from here. See
docs/incidents/gateway-keepalive-result-block-no-recovery.md.

This mirrors an already-shipped pattern in this codebase: the patrol
scheduler force-exits on an unrecoverable wedge (its own consecutive-CDP-
failure and memory-pressure guards in scanner/patrol_scheduler.py) and
relies on the container's `unless-stopped` restart policy (confirmed via
`docker inspect poob --format '{{.HostConfig.RestartPolicy.Name}}'`) to
bring up a clean process. The Discord bot had no equivalent — this module
is that equivalent, for the gateway specifically.

Runs on a plain `threading.Thread`, deliberately NOT asyncio: whatever
wedges the loop or the keep-alive thread must not be able to wedge the
watchdog too.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import TYPE_CHECKING

from poob.utils.logging import get_logger

if TYPE_CHECKING:
    import discord

log = get_logger("discord.gateway_watchdog")


class GatewayWatchdog:
    """Force-exits the process if the Discord gateway goes silent too long.

    Reads the same ``_last_recv`` timestamp py-cord's own
    ``KeepAliveHandler`` watches (``bot.ws._keep_alive._last_recv``,
    ``time.perf_counter()`` seconds) — private library internals, touched
    deliberately because it's the exact ground-truth signal, not a proxy.
    A future py-cord upgrade could rename these; any read failure is
    swallowed per-tick (skip, keep polling) rather than killing the
    watchdog thread, so a broken attribute path fails toward "backstop
    silently does nothing this tick" rather than "backstop disabled
    forever silently" — see ``_last_recv_age_s``.
    """

    def __init__(
        self,
        bot: discord.Client,
        *,
        check_interval_s: float = 15.0,
        # py-cord's own heartbeat_timeout defaults to 60.0s (discord/state.py)
        # -- the library's own KeepAliveHandler already fires its (unbounded,
        # possibly-hanging) recovery attempt at that point. This threshold
        # must be comfortably larger so a NORMAL close+reconnect (a few
        # seconds) never trips it, while still recovering well inside a
        # user-visible "is this thing dead" window.
        silence_threshold_s: float = 150.0,
    ) -> None:
        self._bot = bot
        self._check_interval_s = check_interval_s
        self._silence_threshold_s = silence_threshold_s
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the watchdog thread. Idempotent — safe to call again on
        a reconnect-triggered re-entry into on_ready."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="gateway-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _last_recv_age_s(self) -> float | None:
        """Seconds since the gateway last received any payload, or None
        if the keep-alive handler isn't up yet (still connecting — not an
        error) or its internals couldn't be read this tick."""
        try:
            ws = getattr(self._bot, "ws", None)
            if ws is None:
                return None
            keep_alive = getattr(ws, "_keep_alive", None)
            if keep_alive is None:
                return None
            last_recv = keep_alive._last_recv
        except Exception:
            return None
        return time.perf_counter() - last_recv

    def _run(self) -> None:
        while not self._stop_event.wait(self._check_interval_s):
            age = self._last_recv_age_s()
            if age is None or age <= self._silence_threshold_s:
                continue
            log.critical(
                "Discord gateway silent past threshold — py-cord's own "
                "keep-alive recovery did not complete "
                "(docs/incidents/gateway-keepalive-result-block-no-recovery.md). "
                "Force-exiting so the container restart policy recovers.",
                silence_s=round(age, 1),
                threshold_s=self._silence_threshold_s,
            )
            sys.stderr.flush()
            os._exit(1)
