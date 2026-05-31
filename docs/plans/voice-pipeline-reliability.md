---
type: plan
status: active
date: 2026-05-30
tags: [voice, wake-word, stt, deepgram, reliability, dual-pipeline]
related: [[voice-architecture]] [[wake-word-dual-gate]] [[voice-latency-optimization]] [[text-mode-rlhf-refusal-leak-2026-05-29]] [[dave-version-zero-rejected-by-e2ee-required-guilds]]
---

# Voice pipeline reliability — wake/STT/queue hardening

## Background

A 2026-05-30 audit of ~17h of production voice logs (pulled from `docker logs poob` + `conversation_messages`) surfaced four candidate issues. This plan records the **verified** root cause of each (against the actual source, not the log text alone) and sequences the fixes. The persona/marketplace problems are out of scope here — those landed as Fixes 1–4 in [[text-mode-rlhf-refusal-leak-2026-05-29]].

Wake **model** is healthy and confirmed live: `WAKE_WORD_MODEL_PATH=/app/hey_poob_v3.onnx` — the mass-augmentation v3 ([[wake-word-v3-shipped]], [[wake-word-mass-augmentation-v3]]). "v4" is an unstarted plan ([[wake-word-v4-phonetic-neighbor-followup]]), not deployed. The issues below are in the **runtime pipeline around** the model, not the model itself.

## Findings (verified against source)

### Issue 1 — "Erratic wake latency 325–7602 ms" — NOT A BUG (measurement artifact)

`latency_ms` at [dual_pipeline.py:680-684](../../src/poob/voice/dual_pipeline.py#L680) is `int((now - pipeline.speech_start_time)*1000)`, and `speech_start_time` is set at [dual_pipeline.py:689](../../src/poob/voice/dual_pipeline.py#L689) to **when the user started speaking** (first energy frame). So the metric measures *how far into the utterance the wake phrase matched*, not detection-processing time. A 7.6 s value means "Hey Poob" landed 7.6 s into accumulated speech (long utterance or multi-user crosstalk extending `speech_started`), not slow inference (per-frame inference is sub-100 ms). **Do not chase this as a latency regression.** Action: rename/relabel the metric so it isn't misread (e.g. `speech_to_wake_ms`) and optionally add a separate true-inference-latency counter. Priority: LOW (cosmetic/observability).

### Issue 2 — Lost transcript: "Wake word fired but Deepgram never delivered transcript"

[dual_pipeline.py:845-860](../../src/poob/voice/dual_pipeline.py#L845) `_deferred_emit` waits 15 × 0.1 s = **1.5 s** for a transcript, then **silently drops** the addressed utterance (logs a warning, no user-facing feedback, no response). `get_transcript` already falls back to interim ([dual_pipeline.py:403-421](../../src/poob/voice/dual_pipeline.py#L403)), so a true "never delivered" means **even the interim was empty after 1.5 s** — which most often means Deepgram never received the audio, i.e. the stream was disconnected (Issue 3). So Issue 2 is largely a *symptom* of Issue 3 plus a too-short, fail-silent wait. Priority: MEDIUM. Fix couples to Issue 3.

### Issue 3 — Lazy Deepgram reconnect (up to 5 s STT gap)

[dual_pipeline.py:359-401](../../src/poob/voice/dual_pipeline.py#L359) `send_audio` reconnects only when the *next* audio frame arrives AND ≥5 s since the last attempt ([dual_pipeline.py:385-390](../../src/poob/voice/dual_pipeline.py#L385)). The `_listen_loop` `finally` sets `connected=False` on WS close ([dual_pipeline.py:356](../../src/poob/voice/dual_pipeline.py#L356)) but does **not** proactively reconnect. Consequence: after a disconnect, up to 5 s of speech is dropped before the stream re-establishes; if the user goes quiet, it only reconnects on their next utterance. It **self-heals** (not permanently STT-dead), but the 5 s window loses the start of the next request. The 3-simultaneous-disconnect event (23:50) was likely a Deepgram-side/network blip; need to confirm the users reconnected (next log pull). Priority: HIGH (biggest real UX impact). **Risk/vault note:** the 5 s throttle is a deliberate cost/anti-storm guard — per CLAUDE.md, tightening or adding proactive reconnect is a safety-valve change; document the rationale before flipping. Proposed: on `_listen_loop` exit, schedule a single proactive reconnect (guarded by the existing per-user connect lock) instead of waiting for the next frame, keeping the 5 s throttle as the floor between *attempts*.

### Issue 4 — No session-layer transcript dedup (double-trigger)

The same "Hey Poob play tiki tiki fong" was processed twice ~10 s apart (22:48:33, 22:48:43), both routed to music. The **second produced an empty 433 ms response** — consistent with the music duplicate-play suppression ([[music-tool-call-robustness]], `_is_duplicate_play`, 20 s window in [brain/poob.py](../../src/poob/brain/poob.py)) firing. So for **music**, the double-queue was already prevented. The gap: there is **no dedup at the utterance/transcript layer** ([session.py `_addressed_queue`](../../src/poob/voice/session.py), [dual_pipeline.py `_emit_utterance`](../../src/poob/voice/dual_pipeline.py)). A re-emitted identical transcript for a **non-music** address (e.g. a casual question) would produce a duplicate response. Priority: HIGH-value / LOW-risk — a per-user "last emitted transcript + timestamp" guard at the emit boundary closes it generally, complementing (not replacing) the music dedup.

## Fix sequence

1. **Transcript dedup guard (this PR).** Add a per-user last-emitted `(normalized_transcript, monotonic_ts)` cache in `DualPipelineProcessor`; in `_do_emit` (addressed path), drop an identical transcript seen within a short window (e.g. 8 s) before firing the callback. Low risk, self-contained, TDD-able, directly addresses "no double queues" for all intents (not just music). Does NOT touch the dual-gate or Deepgram lifecycle.
2. **Deepgram reconnect frame-buffering (SHIPPED 2026-05-31).** Operator chose buffer-during-reconnect over tightening the throttle — it preserves the lazy + 5 s-throttle cost guard (no behavior change to *when* we reconnect) and only stops data loss *within* that window. `DeepgramStreamManager` now buffers frames that can't be sent (stream down / connecting / throttled) into a bounded per-user ring (`_PENDING_MAX_FRAMES=150` ≈ 3 s, drops oldest) and flushes them in order via `_flush_pending` the moment the socket is back; connected sends drain stragglers first. Because buffering only happens during active speech (`send_audio` is gated on speech), the ring holds recent contiguous audio, not silence gaps — bounding staleness. Tests: `tests/unit/test_dual_pipeline.py` (bounded ring, in-order flush, re-buffer-on-failure, connected-drain, connected-failure-buffers). This is NOT a safety-valve flip (reconnect timing/cost unchanged), so no separate decision note required — documented here + in the commit. Reduces Issue 2 (lost transcript) as a side effect, since the start-of-utterance audio now reaches Deepgram.
3. **Lost-transcript: fail loud, not silent (DEFERRED — measure first).** Step 2's frame-buffering addresses the *root* of the "never delivered transcript" warning (the start-of-utterance audio now reaches Deepgram), so the silent-drop should become rare. Building a "didn't catch that" clarifier needs new callback plumbing (dual_pipeline → session → a short TTS clip) and would be speculative against a now-reduced problem. Deferred: re-pull logs post-deploy; if `_deferred_emit` timeouts still occur at a meaningful rate, add an `on_lost_transcript` callback then. No code in this pass.
4. **Metric relabel (SHIPPED 2026-05-31).** Renamed the wake log field `latency_ms` → `speech_to_wake_ms` with an inline note that it measures utterance-start→wake-match, not detection latency — so it can't be misread as a perf regression (Issue 1). Single log site, no dependents (grep-confirmed).

## Files likely to change

- [src/poob/voice/dual_pipeline.py](../../src/poob/voice/dual_pipeline.py) — dedup guard (`_do_emit`), proactive reconnect (`_listen_loop`/`send_audio`), deferred-emit behavior, metric label.
- [tests/unit/test_dual_pipeline.py](../../tests/unit/) (new or existing) — dedup + reconnect-trigger tests, mocked at the Deepgram boundary.
- Vault: a gotcha for the reconnect safety-valve change; update [[voice-architecture]] once shipped.

## Out of scope

- Wake-model retraining (v4) — separate effort, [[wake-word-v4-phonetic-neighbor-followup]].
- Wake over/under-firing tuning — the restored model2vec semantic signal ([[text-mode-rlhf-refusal-leak-2026-05-29]] Fix 4) already improves the addressee decision; measure its effect before further tuning.
- DAVE `VOICE_MAX_DAVE_PROTOCOL_VERSION=0` env — harmless (code overrides to 1); one-line homelab env cleanup, not code.
