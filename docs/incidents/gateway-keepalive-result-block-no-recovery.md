---
type: incident
status: resolved
date: 2026-09-05
tags: [discord, gateway, py-cord, watchdog, reliability, voice]
related: [[voice-4014-reconnect-event-loop-wedge]] [[davey-session-needs-serialization-lock]] [[main-browser-cdp-wedge-infinite-hang]]
---

# Discord gateway went silent forever after a routine voice auto-join — py-cord's own keep-alive recovery has no timeout

## Symptom

Live production report ("again not working"), 2026-09-05 ~01:08 UTC. A
text-channel request ("play hunt showdown theme music remix") triggered a
normal auto-join voice connect. 60.15 seconds after the voice handshake
completed:

```
01:08:24.718  Voice handshake complete. Endpoint found ...
01:09:24.869  WARNING [discord.gateway] Shard ID None has stopped
              responding to the gateway. Closing and restarting.
              ───── TOTAL SILENCE from here ─────
```

Confirmed live via SSH, 6+ minutes after the warning: container `Up`, CPU
27-33% (other guild's background threads, not idle-zero), main thread
(`tid=1`) sitting in `epoll_wait` (`/proc/1/task/1/wchan` == `ep_poll` —
genuinely waiting on I/O, not spinning or wedged in a native call), zero
new log lines of any kind (voice, text, patrol — everything). Only a
manual container restart recovered it. `py-spy dump` (the incident-note-
recommended decisive check) was attempted but blocked by the harness's own
safety classifier as a live-process ptrace attach; the diagnosis below was
built from what's readable without it — `/proc/*/wchan`, the exact timing
match, and the installed dependency's own source.

## Root cause

The 60.15s gap is not a coincidence: py-cord's `ConnectionState` default
`heartbeat_timeout` is `60.0` (`discord/state.py:187`), and `_last_recv`
(the timestamp the gateway's `KeepAliveHandler` thread checks) stopped
advancing essentially at the moment the voice handshake completed —
something on the shared event loop went quiet to the *main* gateway too,
for at least that window. Exactly what blocked it first could not be
pinned down without a stack trace (see py-spy note above); what is
provably true from the installed library's own source is that whatever
happened next had **no way to recover**:

`discord/gateway.py`, `KeepAliveHandler.run()` (py-cord 2.7.0, as
installed):

```python
if self._last_recv + self.heartbeat_timeout < time.perf_counter():
    _log.warning("Shard ID %s has stopped responding ... Closing and restarting.", ...)
    coro = self.ws.close(4000)
    f = asyncio.run_coroutine_threadsafe(coro, loop=self.ws.loop)
    try:
        f.result()          # <-- NO TIMEOUT
    except Exception:
        _log.exception(...)
    finally:
        self.stop()
        return
```

`f.result()` blocks the **KeepAliveHandler thread itself**, unconditionally,
until `ws.close(4000)` completes. If that coroutine never completes — e.g.
the underlying transport is already a zombie after whatever caused the
initial silence — this thread hangs forever. Critically, `self.stop()` and
the `return` are inside the block that never runs: the one thread
responsible for ever triggering a fresh reconnect just dies wedged,
silently, with no exception surfaced anywhere. The library's own
self-healing has an unbounded block at exactly the point it needs to be
most defensive (recovering from a gateway that's already showing signs of
trouble). This is a real, cited defect in the installed dependency — not
something patchable from our code.

This is the same *class* of problem as [[main-browser-cdp-wedge-infinite-hang]]
and [[voice-4014-reconnect-event-loop-wedge]]: a wedge with no timeout that
can only be recovered by killing the process. It is genuinely a different
*mechanism* than voice-4014-reconnect-event-loop-wedge's diagnosis,
however — see the correction below.

## A stale vault note nearly sent this fix in the wrong direction

[[voice-4014-reconnect-event-loop-wedge]] (2026-06-01, `status: active`)
describes an unlocked native `davey` (DAVE E2EE) FFI call pinning the
event loop, with a 5-item fix plan, "not yet shipped." Investigating this
incident, that note was the first hypothesis followed — but a direct grep
of the current `src/poob/voice/voice_compat.py` shows items 1-2 of that
plan (`_dave_lock` guarding every native call site; the expensive re-key
native call already wrapped in `loop.run_in_executor`) are **already
fully implemented and correct**. The note's `status: active` was stale
documentation from a fix that shipped without the note being updated —
not a live gap. Re-verified by reading every `_dave_lock` call site
directly; none hold the lock across an `await`.

**Lesson applied from CLAUDE.md's own rule** ("if a recalled memory/doc
conflicts with current information, trust what you observe now"): the vault
pointed at a plausible, well-documented mechanism: it was still worth
citing and checking, but checking it against the actual code (not just
trusting the note) is what caught the dead end before a wrong fix shipped.
[[voice-4014-reconnect-event-loop-wedge]] is being updated to `status:
resolved` for items 1-2, with item 5 (the watchdog) marked fulfilled by
this incident's fix.

## Fix

Item 5 of that same old plan — "a bot-process loop-liveness watchdog...
the only guaranteed recovery if a future native wedge slips past" — was
the one piece never shipped, and is the correct backstop here regardless
of what specifically caused the initial 60s of silence: **`GatewayWatchdog`**
(`src/poob/discord_bot/gateway_watchdog.py`), a plain `threading.Thread`
(deliberately not asyncio, so it can't be wedged by whatever wedges the
loop or the keep-alive thread itself). It polls the same
`bot.ws._keep_alive._last_recv` timestamp py-cord's own KeepAliveHandler
watches, and if gateway silence exceeds `silence_threshold_s` (default
150s — comfortably past py-cord's own 60s `heartbeat_timeout` plus margin
for a normal close+reconnect), force-exits the process via `os._exit(1)`.

This mirrors an already-shipped, proven pattern in this exact codebase:
the patrol scheduler's `_record_cycle_outcome` / `_maybe_restart_on_memory_pressure`
(`scanner/patrol_scheduler.py`) do the identical thing for a wedged
browser CDP session, relying on the container's `unless-stopped` restart
policy (confirmed live: `docker inspect poob --format
'{{.HostConfig.RestartPolicy.Name}}'` → `unless-stopped`) to bring up a
clean process. The Discord bot had no equivalent; now it does.

Wired into `ScraperBot.__init__` (constructed once) and started from
`on_ready` (`self._gateway_watchdog.start()`, idempotent — safe against
the repeat `on_ready` calls a reconnect triggers).

## Validation

- `tests/unit/test_gateway_watchdog.py` (6 tests): force-exits past
  threshold; never exits while the gateway keeps ticking; no-ops before
  `bot.ws` exists (still connecting); `stop()` actually ends the thread;
  `start()` is idempotent; a broken/renamed internals path (future py-cord
  upgrade) fails safe (skips the tick) rather than killing the watchdog
  thread.
- Mutation-verified: replaced the threshold check with an unconditional
  `continue`, confirmed exactly the positive test failed (others
  unaffected), restored, confirmed all 6 pass again.
- `tests/unit/test_bot_gateway_watchdog_wiring.py`: structural guard that
  the constructor actually attaches a `GatewayWatchdog` and `on_ready`
  actually calls `.start()` on it — the same "wiring gap" class of bug as
  [[voice-llm-model-deprecated-and-never-wired]] (a thing that exists in
  code but is never connected). Mutation-verified: removed the
  `.start()` call, confirmed the wiring test failed, restored, confirmed
  pass.
- Full unit suite: 1976 passed, 1 skipped, no regressions.
- `ruff check` / `ruff format` clean on all authored files (pre-existing
  lint/format findings in `bot.py` outside the touched lines were left
  alone — confirmed pre-existing via `git stash` diff before/after).

## Follow-ups

- The exact trigger for the initial ~60s of event-loop silence (before the
  keep-alive's own recovery additionally wedged) was not conclusively
  identified — `py-spy dump` was blocked by the harness's safety
  classifier as a live-process ptrace attach, and the process had already
  moved past that window (idle epoll) by the time investigation reached
  it. The watchdog fixes the "never recovers" half unconditionally,
  regardless of root trigger; if this recurs, a `py-spy dump` taken
  *during* the initial silence window (not after) would be decisive, or
  file an issue against py-cord for the unbounded `f.result()`.
- Consider upstreaming a timeout fix to py-cord's `KeepAliveHandler.run()`
  (`f.result(timeout=...)` instead of unbounded `f.result()`) — out of
  scope here, but the actual defect lives there.
