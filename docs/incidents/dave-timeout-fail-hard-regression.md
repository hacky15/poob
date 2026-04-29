---
type: incident
status: resolved
date: 2026-04-29
tags: [voice, dave, pycord, regression, vault-process]
related: [[pycord-dave-migration]] [[voice-architecture]] [[voice-three-failure-modes-april27]]
---

# DAVE-timeout fail-hard regression — every /join auto-disconnected

## Symptom

Right after `a2d4041` deployed:

- Poob joined the voice channel.
- ~15 seconds later, log fired: *`DAVE handshake did not complete in time — refusing to start recording...`*
- Session was torn down, `vc.disconnect(force=True)` was called.
- Poob auto-left without ever speaking the entrance catchphrase.
- Every `/join` repeated the same disconnect cycle.

User: *"poob isnt saying his wake word on join. he also just isnt working... we are in a worst spot than before. its just auto leaving now."*

## Root cause

Commit `a2d4041` changed the DAVE-not-ready branch from *"log warning, start recording anyway"* to *"refuse to start recording, force-disconnect, surface the failure to the user."*

That's wrong for this deployment. Empirical reality:

- `dave_session.ready` can stay `False` for the entire call window on this homelab + Discord-region path.
- Despite that, audio decoding and STT have been working all along (passive transcripts continued to flow, wake words fired, replies happened). Some early frames produce harmless `discord.opus "corrupted stream"` warnings until keys land; the decoder catches up.
- The original code already accepted this with *"DAVE not ready after 5s, starting anyway."* That tradeoff was deliberate and load-bearing — the `ready` flag is a poor proxy for "audio works."

I treated the warning as a fatal condition without verifying the ground-truth behavior of `dave_session.ready` in this codebase, and without reading the existing fall-through code's intent.

## What I should have done first

Per the project conventions in [.claude/CLAUDE.md](../../.claude/CLAUDE.md):

> Before any non-trivial work:
> 1. Start at `docs/INDEX.md`.
> 2. Check `docs/gotchas/` first — if someone already learned what you're about to try, don't re-learn it.
> 3. Read the relevant `architecture/` note for mental model, and any `decisions/` notes tagged with the subsystem you're touching.
> 4. If a past `incidents/` note is adjacent, read it — the symptom might match again.

I touched `voice_cog.py` DAVE-handshake logic without reading [[voice-architecture]] (mentions DAVE patch + `voice_compat.py`), and without searching docs for "DAVE" — which would have surfaced the migration history and the deliberate "start anyway" behavior. I treated the existing warning as buggy when it was a documented safety valve.

## Fix

Revert (`bb51d8e`) to the original log-and-continue behavior. Keep the timeout bump 5s → 15s — that part was a genuine improvement (the legitimate ready-by-then path now lands inside the window).

```python
if not dave_ready:
    log.warning(
        "DAVE not ready within window — starting "
        "recording anyway; decoder catches up once "
        "keys arrive.",
        timeout_s=DAVE_TIMEOUT_S, dave_version=dave_version,
    )
```

The opus `corrupted stream` warnings during early handshake remain. They are noise, not failure — see the gotcha below.

## The durable lesson

Two layers of defense added so the same mistake can't recur:

1. **Gotcha note** [[dave-ready-flag-is-not-truth]] capturing the specific signal: `dave_session.ready` is unreliable on this deployment; do not use it as a fatal precondition. Auto-discovered next time anyone greps `docs/gotchas/` for "DAVE."
2. **Agent contract update** in [.claude/CLAUDE.md](../../.claude/CLAUDE.md): an explicit rule that any change altering existing fall-through / safety-valve behavior must cite a vault note proving the existing path is wrong, not just that it *looks* wrong. "Make it fail-hard" is not a robustness improvement when the existing soft-fail is load-bearing.

## Validation

- `bb51d8e` reverts the destructive branch.
- Subsequent `/join` should produce: connect → 15s DAVE wait → "DAVE not ready..." warning (acceptable) → `Recording started` → `Entrance catchphrase played`.
- Opus corrupted-stream warnings will reappear briefly during the handshake gap — expected, do not fix.
