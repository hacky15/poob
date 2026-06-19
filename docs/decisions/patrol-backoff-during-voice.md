---
type: decision
status: active
date: 2026-06-19
tags: [scanner, patrol, voice, cpu, performance, resource-contention]
related: [[voice-architecture]] [[voice-pipeline-cold-start-drops-requests]] [[homelab-host-memory-observability]] [[in-process-browser-recycle]]
---

# Patrol backs off while users are in a voice channel (CPU priority to voice)

## Context

Operator (2026-06-19): "incredible delay, incredible wake-word inaccuracy — is
Poob deteriorating?" Diagnosis (via the [[voice-pipeline-cold-start-drops-requests]]
wake-gate instrumentation + host inspection): **not code rot — CPU saturation.**

- The homelab is **CPU-only, 4 cores** ([[homelab-cpu-only]]). During a busy
  3-user VC the 1-min load hit **11.69** (~3× over), and the **`poob` container
  itself was at ~229% CPU** — because it runs the **real-time voice pipeline
  AND the marketplace patrol scanner in one process**. A chromium patrol cycle
  (browser automation + enrichment + VLM) competes with voice inference
  (OpenWakeWord per-user, Deepgram STT, Model2Vec, TTS) for the same starved
  cores.
- Measured cost to voice: `speech_to_wake_ms` **3842–4196ms** (should be
  ~50–150ms), Deepgram streams **zombie out** ("never delivered transcript"),
  utterances stretch to 16s. Load is transient — **2.39 when VC is idle**,
  spiking only under active multi-user voice — confirming the contention model.
- Co-tenants (permitscope, komodo, CI runners) were only ~35% combined, so
  **Poob's own scanner-vs-voice contention is the dominant, in-our-control
  lever.**

## Decision

While users are **actively** in a voice channel, the patrol scheduler **skips
the cycle** so real-time voice wins the shared CPU. Once voice is quiet for a
window, the scanner resumes full-tilt. **No scanner functionality is removed**
— cycles are deferred, not dropped; outside VC nothing changes.

### Design — a decoupled voice-activity beacon

The voice pipeline and the scanner must not reference each other, so the signal
is a single process-wide module-level timestamp in a neutral location:

- [`utils/voice_activity.py`](../../src/poob/utils/voice_activity.py):
  `mark_active(ts)` / `is_active(window_s)` / `seconds_since_active()`. A single
  float write (atomic under the GIL) — no objects, callbacks, or locks.
- **Producer:** `DualPipelineProcessor.process_audio_frame` calls
  `mark_active(pipeline.last_audio_time)` on the per-frame audio hot path,
  reusing the timestamp it already takes (no extra syscall). Discord only sends
  frames while a user transmits, so this marks exactly when voice is consuming
  CPU.
- **Consumer:** `PatrolScheduler._should_skip_for_voice()` (checked in `_loop`
  right after the `_is_paused` gate) returns True while
  `voice_activity.is_active(window)`.
- **Config:** `patrol_skip_during_voice` (default True) +
  `patrol_voice_activity_window_s` (default 120s — the scanner won't start a
  cycle unless there's been no voice audio for 2 min, so conversational pauses
  don't let a 10-min cycle start mid-conversation).

### Why skip *before* the recycle/memory-guard/cycle

The skip is placed before `maybe_recycle_browsers` + `_maybe_restart_on_memory_pressure`
+ `run_patrol_cycle`. This is deliberate: it not only avoids the cycle's CPU, it
also stops a **memory-pressure process restart from firing mid-VC and killing
the live voice session.** Skips are a `continue` — they do NOT count as cycle
failures (`_record_cycle_outcome` isn't called), so the consecutive-failure
force-exit guard is unaffected. An idle scanner doesn't navigate pages, so the
chromium leak doesn't grow while skipping.

## Alternatives considered

- **Globally slow the patrol interval:** penalizes the scanner even when no one
  is in VC. Rejected — the win is *conditional* backoff.
- **Reduce per-cycle concurrency during VC:** still runs chromium (the hog) and
  is more complex than skipping. Rejected for v1.
- **Drop OpenWakeWord to free CPU:** OWW is unreliable (scores ~0.998 on
  everything — see [[voice-pipeline-cold-start-drops-requests]]) but it's an
  *accuracy* problem, not the CPU hog; keep it, fix via a v4 retrain.
- **Infra (more cores / offload co-tenants):** the durable fix, but an
  operator/host action. This decision is the $0, in-code mitigation; infra
  remains the backstop if 3-user voice alone still saturates 4 cores.

## Consequences

- Busy VC → patrol defers → voice gets the CPU → wake latency + STT recover.
- Long VC sessions stall the scanner until quiet; acceptable (deals aren't
  latency-critical; operator approved the trade). Telemetry: `consecutive_voice_skips`
  in the skip log surfaces if the scanner is being starved for very long.
- One flip (`patrol_skip_during_voice=False`) fully restores prior behavior.

## Validation

`tests/unit/test_voice_activity_beacon.py`: beacon default-inactive / mark /
window-expiry; `process_audio_frame` marks (grep) + `_loop` consults the skip
(grep); and the decision itself — skips when active, runs when idle, runs when
the window expired, and never skips when the flag is off (no-loss revert). Full
unit suite green.

## Follow-ups

- **Wake accuracy:** v4 OpenWakeWord retrain (the model fires ~1.0 on
  everything); the `oww_peak` instrumentation now provides the data.
- **Infra:** if voice-alone still saturates 4 cores, move permitscope/CI off the
  box or add cores ([[homelab-host-memory-observability]]).
