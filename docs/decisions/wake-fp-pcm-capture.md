---
type: decision
status: active
date: 2026-04-23
tags: [voice, wake-word, training, data-capture]
related: [[wake-word-dual-gate]] [[wake-gate-over-rejection-during-music]] [[voice-architecture]]
---

# Capture wake-word false-positive PCM for offline retraining

## Context

`openwakeword` with our custom `hey_poob.onnx` fires on phrases that *sound like* "hey poob" — "hey poop", "pooch", "pomp", common sibling-phoneme utterances. The current pipeline rescues most of these via the text-gate: `Audio wake word overridden by text (no match in transcript)` logs whenever acoustic fires but the transcript doesn't contain the wake word.

Every one of those events is a **real-world hard negative** — the exact game-audio / crosstalk / friends-in-VC acoustic environment our model needs to learn to reject. Synthetic TTS confusables (via `piper-sample-generator`) cover breadth; production false positives cover authenticity. We want both for the next training run.

## Decision

Add opt-in PCM capture keyed off an env var.

**Runtime behavior:**

- `DualPipelineProcessor` keeps a per-user rolling 2-second ring buffer of 16 kHz mono PCM (100 frames × 20ms). Each frame arriving via `process_audio_frame` is appended.
- On the `Audio wake word overridden by text (no match in transcript)` event, the current contents of that user's ring are concatenated and written to `<WAKE_FP_CAPTURE_DIR>/<UTCtimestamp>_<user>_<user_id>.wav`.
- A `Wake-word FP captured` log line records the filepath and transcript so you can correlate.

**Enable/disable:**

- `WAKE_FP_CAPTURE_DIR=/app/data/wake_fp` in the Komodo stack env enables capture and auto-creates the directory.
- Unset / empty = no-op. Zero cost when off — the ring buffer is only created under the guard.

**Memory cost when on:** ~64 KB per user in VC (100 frames × 640 bytes). 5 users = ~320 KB. Disk cost: each WAV ≈ 64 KB. A typical false-positive rate of ~2 per minute in noisy sessions gives ~2-3 MB per hour of voice time.

## Rationale

- Training data quality > quantity for hard negatives. One hour of Ben's actual Discord voice call during a noisy game is worth more than 10,000 synthetic TTS confusables.
- The capture is passive — we don't change any detection logic. If the capture code breaks, wake detection keeps working.
- Keying off an env var keeps the default behavior unchanged and the capture path trivially toggleable.

## Exit criterion

After a week of normal use with capture enabled:

- Confirm the folder is populated (non-zero WAVs).
- Listen to a few samples — they should contain audio that actually sounds like wake-word confusables, not silence or unrelated speech.
- Spot-check that WAV files are well-formed (16 kHz mono 16-bit).

## Rollback

Unset `WAKE_FP_CAPTURE_DIR`. Delete the accumulated WAVs if wanted.

## Follow-up (not part of this change)

Once we have a week of captures:

1. Combine with synthetic confusables from `piper-sample-generator`.
2. Retrain via `livekit-wakeword` or `openwakeword` training pipeline on your 2070.
3. Drop new ONNX into `data/hey_poob.onnx`.
4. Redeploy, evaluate false-positive rate.

Opens decision `docs/decisions/wake-word-retrain-livekit.md` when we're ready.

## Results

<!-- Fill in with capture volume stats once a week of data exists. -->
