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
        # 2026-09-10: this exact incident recurred, and the FIRST question
        # in triage -- "did the watchdog even run?" -- was unanswerable from
        # logs alone. A silently-doing-nothing watchdog (never started, or
        # perpetually unable to read _last_recv) is indistinguishable from a
        # correctly-running one that just never saw silence, UNLESS it
        # periodically proves it's alive. heartbeat_every_n_ticks controls
        # that cadence; unreadable_warn_after_s bounds how long the "can't
        # read gateway state" case stays silent before it becomes a WARNING
        # instead of an indefinite no-op.
        heartbeat_every_n_ticks: int = 20,
        unreadable_warn_after_s: float = 150.0,
    ) -> None:
        self._bot = bot
        self._check_interval_s = check_interval_s
        self._silence_threshold_s = silence_threshold_s
        self._heartbeat_every_n_ticks = heartbeat_every_n_ticks
        self._unreadable_warn_after_s = unreadable_warn_after_s
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick_count = 0
        self._none_streak = 0
        self._warned_unreadable = False

    def start(self) -> None:
        """Start the watchdog thread. Idempotent — safe to call again on
        a reconnect-triggered re-entry into on_ready."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="gateway-watchdog", daemon=True)
        self._thread.start()
        log.info(
            "GatewayWatchdog started",
            check_interval_s=self._check_interval_s,
            silence_threshold_s=self._silence_threshold_s,
        )

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
            self._tick_count += 1
            age = self._last_recv_age_s()

            if age is None:
                self._none_streak += 1
                unreadable_s = self._none_streak * self._check_interval_s
                if not self._warned_unreadable and unreadable_s > self._unreadable_warn_after_s:
                    self._warned_unreadable = True
                    log.warning(
                        "GatewayWatchdog cannot read gateway keep-alive state "
                        "(bot.ws._keep_alive._last_recv) -- either still "
                        "connecting for an unusually long time, or a py-cord "
                        "internals change broke the attribute path. The "
                        "watchdog is effectively blind until this clears.",
                        unreadable_for_s=round(unreadable_s, 1),
                    )
                continue
            self._none_streak = 0
            self._warned_unreadable = False

            # Proof-of-life: with zero periodic output, a watchdog that never
            # started and one that's running and simply never seeing silence
            # are indistinguishable after the fact. See docs/incidents/
            # gateway-keepalive-result-block-no-recovery.md's 2026-09-10
            # addendum.
            if self._tick_count % self._heartbeat_every_n_ticks == 0:
                log.info(
                    "GatewayWatchdog heartbeat",
                    last_recv_age_s=round(age, 1),
                    threshold_s=self._silence_threshold_s,
                )

            if age <= self._silence_threshold_s:
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
