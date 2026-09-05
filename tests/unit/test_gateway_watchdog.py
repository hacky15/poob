"""TDD for the Discord gateway liveness watchdog.

Production, 2026-09-05 ~01:08-01:14 UTC: a text-channel "play hunt showdown
theme music remix" request triggered a voice auto-join; ~60.15s after the
voice handshake completed, py-cord's own KeepAliveHandler thread logged
"Shard ID None has stopped responding to the gateway. Closing and
restarting." (matching its hardcoded 60.0s heartbeat_timeout default,
discord/state.py:187) -- and then NOTHING. No reconnect, no further log of
any kind, for 6+ minutes, confirmed via SSH: container alive, CPU non-zero,
main thread idle in epoll (`/proc/1/task/1/wchan` == `ep_poll`), zero new
log lines. Only a process restart recovers.

Root cause of "zero recovery, forever": the installed py-cord's own
recovery path (`discord/gateway.py` KeepAliveHandler.run(), lines ~151-162)
does:

    coro = self.ws.close(4000)
    f = asyncio.run_coroutine_threadsafe(coro, loop=self.ws.loop)
    try:
        f.result()          # <-- NO TIMEOUT
    ...
    finally:
        self.stop()
        return

If `ws.close(4000)` never completes (e.g. the underlying transport is
already a zombie after a silent network drop), `f.result()` blocks the
KeepAliveHandler thread FOREVER, so `self.stop()` and the thread's own
`return` -- the only things that would let a fresh KeepAliveHandler spin up
on reconnect -- never execute. The library's built-in recovery has no
bound. This is a real, cited defect in the installed dependency, not
something patchable at our layer.

This exactly matches the gap already flagged (but never shipped) in
docs/incidents/voice-4014-reconnect-event-loop-wedge.md item 5: "a
bot-process loop-liveness watchdog ... the only guaranteed recovery if a
future native wedge slips past" the other fixes. The DAVE serialization
lock (items 1-2 of that plan) turned out to already be shipped and
verified in src/poob/voice/voice_compat.py -- the incident's `status:
active` was stale documentation, not a live gap. This watchdog is the one
piece of that plan that was actually still missing, and is independent of
whatever specific mechanism wedges the gateway or its own recovery -- it
is a pure backstop, exactly like the patrol scheduler's existing
`_record_cycle_outcome` / `_maybe_restart_on_memory_pressure` (both also
just `os._exit(1)` under a container `unless-stopped` restart policy,
confirmed live via `docker inspect poob`).
"""

from __future__ import annotations

import time
from unittest.mock import patch

from poob.discord_bot.gateway_watchdog import GatewayWatchdog


class _FakeKeepAlive:
    """Stand-in for py-cord's KeepAliveHandler exposing only the one
    attribute the watchdog reads. A plain class (not MagicMock) so tests
    can give ``_last_recv`` real, deterministic dynamic behavior (a live
    callable, or a raising property) without fighting Mock's own
    attribute-interception machinery."""

    def __init__(self, last_recv: float) -> None:
        self._last_recv = last_recv


class _FakeWs:
    def __init__(self, keep_alive: _FakeKeepAlive | None) -> None:
        self._keep_alive = keep_alive


class _FakeBot:
    def __init__(self, ws: _FakeWs | None) -> None:
        self.ws = ws


def _bot_with_last_recv(last_recv: float | None) -> _FakeBot:
    """A minimal stand-in for discord.Client exposing only what the
    watchdog reads: bot.ws._keep_alive._last_recv (perf_counter seconds)."""
    if last_recv is None:
        return _FakeBot(ws=None)
    return _FakeBot(ws=_FakeWs(_FakeKeepAlive(last_recv)))


def test_force_exits_when_gateway_silent_past_threshold() -> None:
    """The whole point of the watchdog: if the gateway hasn't received
    anything in longer than the threshold, force-exit so the container
    restart policy (confirmed `unless-stopped` on the live host) recovers."""
    stale_recv = time.perf_counter() - 1000.0  # far past any threshold
    bot = _bot_with_last_recv(stale_recv)
    watchdog = GatewayWatchdog(bot, check_interval_s=0.01, silence_threshold_s=0.05)

    with patch("poob.discord_bot.gateway_watchdog.os._exit") as mock_exit:
        watchdog.start()
        try:
            deadline = time.monotonic() + 2.0
            while not mock_exit.called and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            watchdog.stop()

    assert mock_exit.called, "watchdog never force-exited on a stale gateway"
    mock_exit.assert_called_with(1)


def test_does_not_exit_while_gateway_is_healthy() -> None:
    """A gateway that keeps ticking (last_recv advancing) must never trip
    the watchdog -- this is the mutation-test partner: without it, a
    watchdog that always force-exits regardless of state would also pass
    the positive test above."""

    class _TickingKeepAlive:
        """_last_recv is read fresh on each watchdog tick via a live
        property, simulating a gateway whose KeepAliveHandler keeps
        calling .tick()."""

        @property
        def _last_recv(self) -> float:
            return time.perf_counter()

    bot = _FakeBot(ws=_FakeWs(_TickingKeepAlive()))
    watchdog = GatewayWatchdog(bot, check_interval_s=0.01, silence_threshold_s=0.1)

    with patch("poob.discord_bot.gateway_watchdog.os._exit") as mock_exit:
        watchdog.start()
        try:
            time.sleep(0.3)
        finally:
            watchdog.stop()

    assert not mock_exit.called, "watchdog force-exited despite a healthy, ticking gateway"


def test_no_op_before_gateway_connected() -> None:
    """bot.ws is None before the first connection completes -- the watchdog
    must tolerate that quietly (not crash, not false-positive exit) rather
    than assume 'no ws' means 'dead gateway'."""
    bot = _bot_with_last_recv(None)
    watchdog = GatewayWatchdog(bot, check_interval_s=0.01, silence_threshold_s=0.05)

    with patch("poob.discord_bot.gateway_watchdog.os._exit") as mock_exit:
        watchdog.start()
        try:
            time.sleep(0.2)
        finally:
            watchdog.stop()

    assert not mock_exit.called


def test_stop_halts_the_thread() -> None:
    """stop() must actually end the background thread -- otherwise every
    test in this file (and every real process restart in tests) leaks a
    daemon thread that keeps polling forever."""
    bot = _bot_with_last_recv(time.perf_counter())
    watchdog = GatewayWatchdog(bot, check_interval_s=0.01, silence_threshold_s=100.0)
    watchdog.start()
    thread = watchdog._thread
    assert thread is not None and thread.is_alive()

    watchdog.stop()
    thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_start_is_idempotent() -> None:
    """start() called twice (e.g. a reconnect re-entering on_ready) must
    not spawn a second watchdog thread."""
    bot = _bot_with_last_recv(time.perf_counter())
    watchdog = GatewayWatchdog(bot, check_interval_s=0.01, silence_threshold_s=100.0)
    watchdog.start()
    first_thread = watchdog._thread
    watchdog.start()
    watchdog.stop()
    if first_thread:
        first_thread.join(timeout=2.0)

    assert watchdog._thread is first_thread


def test_exception_reading_last_recv_does_not_crash_watchdog_thread() -> None:
    """A future py-cord upgrade could rename/remove `_keep_alive._last_recv`.
    The watchdog reaches into private library internals by necessity (see
    module docstring) -- a broken attribute path must fail safe (skip that
    tick, keep polling) rather than kill the watchdog thread silently,
    which would silently disable the whole backstop."""

    class _BrokenKeepAlive:
        @property
        def _last_recv(self) -> float:
            raise AttributeError("renamed in upgrade")

    bot = _FakeBot(ws=_FakeWs(_BrokenKeepAlive()))
    watchdog = GatewayWatchdog(bot, check_interval_s=0.01, silence_threshold_s=0.05)

    with patch("poob.discord_bot.gateway_watchdog.os._exit") as mock_exit:
        watchdog.start()
        try:
            time.sleep(0.2)
            assert watchdog._thread is not None and watchdog._thread.is_alive(), (
                "watchdog thread died on an internals-access exception -- "
                "the backstop is now silently disabled"
            )
        finally:
            watchdog.stop()

    assert not mock_exit.called
