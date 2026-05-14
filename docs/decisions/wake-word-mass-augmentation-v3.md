---
type: decision
status: active
date: 2026-05-13
tags: [voice, wake-word, training, augmentation, openwakeword]
related: [[wake-word-dual-gate]] [[wake-word-latency]] [[wake-fp-pcm-capture]] [[voice-architecture]]
---

# Wake-word v3 — recall-biased corpus with phonetic-neighbor positives + clip augmentation

## Context

Production failure data on 2026-05-13 02:15-02:19 showed back-to-back wake-word failures from real users (Owen, Ben, Lab Rat, Roner). Three failure modes dominated:

1. **STT-garbled prefix.** User says "Hey Poob"; Deepgram writes "A Poob" or "Pay Poob". Acoustic openwakeword either misses or fires but gets overridden by the strict text gate (`\bhey[\s,.]+(?:p[ou]{1,2}b|...)\b`).
2. **Bare wake.** User drops "Hey" entirely. "Poob, Nightcore." → fails. Current positives ship no bare-stem audio.
3. **Phonetic-neighbor mishears.** Fast / mumbled / accented "Hey Poob" embeds acoustically as something STT writes as "Hey Noob", "Hey Tube", "Hey Poop". The wake model sees one audio embedding; STT writes another spelling; current training has no positives for these.

[[wake-word-dual-gate]] documents the gate semantics, but no gate change rescues an acoustic miss — the acoustic model has to fire. The research in [[wake-word-augmentation-2026]] identifies onset-clipping + phonetic-neighbor positives + multi-prefix variants as the highest-leverage missing augmentations.

The user's explicit directive (2026-05-13): *"we need to train on everything that could possibly be 'hey poob'. things like 'hey poob' but have the audio cut off at the beginning slightly. at the end slightly. even a split second cut in the audio in the middle so it comes through like 'hey oob' and everything in between. we need to go heavy on these positives. different voices. different words. different speeds. different tones. over and over."*

## Decision

Ship a v3 corpus-generation pipeline under `scripts/wake_word_v3/` that produces **~4.5M+ positive WAVs and ~38k adversarial negatives** via combinatorial expansion of:

- **449 phrase variants** (see `scripts/wake_word_v3/phrases.py`):
  - 13 canonical "Hey Poob" forms (oversampled for weight).
  - 34 prefix variants: `A Poob`, `Pay Poob`, `Hi Poob`, `Yo Poob`, `Eh Poob`, `Ey Poob`, `Ay Poob`, `Bay Poob`, `Day Poob`, `Way Poob`, `Stay Poob`, `Heya Poob`, `Hey there Poob`, `Okay Poob`, `Hmm Poob`, etc.
  - **65 phonetic-neighbor positives** (the user's specific request): `Hey Noob`, `Hey Boob`, `Hey Tube`, `Hey Poop`, `Hey Loop`, `Hey Pub`, `Hey Pooh`, `Hey Pewb`, `Hey Pooba`, `Hey Pooby`, etc. covering the `/uːb/`, `/uːp/`, `/uːb/`, `/ʌb/` rime classes and accent renderings.
  - 296 trailing-context permutations (8 prefix templates × 37 continuations).
  - 31 bare-stem variants: `Poob`, `Poob, play music`, `Poob nightcore it`, `Boob,`, `Noob,`.
  - 10 slurred forms: `Heypoob`, `Hipoob`, `Yopoob`, etc.
- **47 Edge TTS English voices × 8 speaking rates** = 376 acoustic identities per phrase. Accent coverage: US, GB, AU, CA, IN, IE, HK, KE, NZ, NG, PH, SG, ZA, TZ.
- **Per-sample clip augmentation** (`scripts/wake_word_v3/clip_augment.py`): 5 head-clip lengths × 1 + 5 tail-clip lengths × 1 + 5 mid-dropout lengths × 3 positions + 2 mid-cut lengths = **27 augmented variants per base WAV**.

Final shape:

```
data/wake_word_training_v3/
  pos/             ~169k base positive WAVs (449 × 47 × 8)
  pos_augmented/   ~4.5M clip-augmented positive WAVs (169k × 27)
  neg/             ~38k adversarial negative WAVs (101 × 47 × 8) — no augmentation
```

## Why phonetic neighbors as POSITIVES (not negatives)

This is the strongest design decision and deserves its own rationale: the user's intuition is correct, and the research backs it within bounded scope.

**The model sees audio embeddings, not transcripts.** When a user mumbles "Hey Poob" fast / accented / in noise, the audio envelope often embeds closer to one of the phonetic neighbors than to the canonical pronunciation. STT (Deepgram, downstream of the wake decision) chooses ONE textual spelling; the wake decision happens on raw embeddings before STT has any opinion.

If we train "Hey Noob" / "Hey Boob" / "Hey Tube" as **negatives**, we tell the model: this audio envelope is NOT a wake. That's the wrong signal — the audio envelope from a real user saying "Hey Poob" mumbled is materially the same envelope. The model learns to suppress its own user's voice.

If we train them as **positives**, we tell the model: this audio envelope IS a wake. The cost is that someone conversationally calling another user "a noob" will trigger a false positive. We accept that:

- [[wake-word-dual-gate]] catches the most pernicious FP class (Deepgram-hallucinated "Hey Poob" during bot audio loopback). Loose-text + audio gates filter conversational FPs at the text layer.
- This bot serves a small private Discord; FP cost is low. Recall cost is high.

The [[wake-word-augmentation-2026]] survey notes that pure phonetic-neighbor positives without paired hard negatives risk inflating bare-stem FPs. Mitigation: ship a parallel adversarial-negative set explicitly covering conversational use of those same neighbors (`"to the pub"`, `"what a noob"`, `"swimming pool"`, `"the tube"`). Trained together, the model learns the envelope-vs-context distinction the per-word labels imply.

This is a **recall-biased** training corpus. FP rate is expected to rise modestly; the dual-gate eats most of that. If post-deploy FP/hr exceeds the user's tolerance, fork to the two-model ensemble (full-phrase strict + bare-stem with elevated hard-negative weight) recommended in section 7 of [[wake-word-augmentation-2026]].

## Clip augmentation specifics

Three production failure modes → three augmentation primitives:

| Mode | What it models | Sizes |
|---|---|---|
| **Head clip** | VAD truncates onset; packet loss at start; STT writes "A Poob" because the "Hey" audio never reached the model | 30, 60, 100, 150, 200 ms removed from start |
| **Tail clip** | Trailing silence cropped; speaker cut off mid-sentence | 50, 100, 150, 200, 250 ms removed from end |
| **Mid dropout** | Discord packet-loss glitch; brief mic dropout; preserves duration with silence | 30, 50, 70, 100, 150 ms × position 0.3 / 0.5 / 0.7 |
| **Mid cut** | Packet loss with immediate resync; collapses the dropped window | 50, 100 ms × position 0.5 |

All operations work on raw int16 PCM at 16 kHz mono via byte-slicing — no scipy/librosa dependency. Pipeline is pure stdlib + ffmpeg shell-outs so it can run inside any environment that already has ffmpeg.

## Alternatives considered

- **Lower openwakeword threshold (0.7 → 0.5).** Vault [[wake-gate-over-rejection-during-music]] explicitly documents that 0.7 was raised FROM 0.5 because the lower threshold caused FPs in multi-user calls. Re-lowering is a documented regression. Rejected.
- **Train on real user recordings only.** Not enough volume; bot has been live ~2 months with ~hundreds of real wake utterances available. A few hundred reals + 4.5M synthetics is the ratio Synth4Kws + LLM-Synth4KWS recommend for low-resource personal wake words.
- **Add Piper + Coqui XTTS-v2 + ElevenLabs.** Recommended by research for engine-diversity gains. Deferred to v4: Edge TTS alone covers 47 voices × 11 regions; if recall remains weak after v3 deploys, add a second engine.
- **Speed perturbation via ffmpeg/sox at the post-process layer.** Edge TTS `rate` parameter already covers `-30%` to `+30%` in 8 steps; doubling the speed perturbation at the post-process step would explode disk usage 3× for marginal extra coverage. Deferred to v4.
- **Pitch perturbation at post-process.** OpenWakeWord's `augment_clips()` already applies ±3 semitones at p=0.25 during training. Doing it pre-augmented would double disk usage; the in-training augmentation already handles it.
- **GAN-based adversarial augmentation.** [[wake-word-augmentation-2026]] explicitly flags this as low-ROI for small custom wake words.
- **Architecture swap to Conformer / MatchboxNet.** Research consensus: data improvements dominate architecture improvements at this scale. Don't switch architectures to fix a data problem.

## Consequences

- **Disk footprint:** ~4.5M positive WAVs at ~1 second × 32 KB/sec (16 kHz mono 16-bit) ≈ **144 GB raw**. Operator should plan for ~200 GB free on the WSL2 dev disk before starting a full run. Or use `--augment-fraction 0.25` for a still-massive ~1.1M positives at 36 GB.
- **Generation runtime:** Edge TTS at 8 concurrent requests ≈ 4-6 samples/sec sustained → **~168k base TTS samples ≈ 8-12 hours**. Clip augmentation is local CPU work, much faster: ~30k samples/min × 4.5M ≈ **2-3 hours**. Total wall-clock: **~12-15 hours** for the full run on a typical homelab.
- **Training runtime:** openWakeWord's feature extraction over 4.5M positives ≈ another **8-12 hours** in WSL2. Training itself (`scripts/train_hey_poob_v2.py` with `STEPS=50000`) ≈ **24-48 hours** on CPU; ~6-10 hours on a single 4090. The user's "days" intent maps to this combined runtime.
- **Resumability:** filenames are deterministic on `(prefix, voice, rate, phrase)` for base and `(base + variant_tag)` for augmentations. Re-running picks up cleanly. The operator can kill the process anytime and resume.
- **FP rate at deploy:** expected to rise. Production monitoring should watch:
  - `Audio wake word overridden by text` event count per hour (gate-blocked FPs)
  - `wake_word=True` event count on transcripts that don't contain "poob" stem (true FPs)
  - Owner complaint volume in #poob-feedback or DMs
- **Reversibility:** the v2 onnx model stays in `data/hey_poob.onnx`. v3 model lands at `data/hey_poob_v3.onnx`. Swap via env var (`WAKE_WORD_MODEL_PATH=/app/data/hey_poob_v3.onnx`). Rollback is a one-env-var change.

## Validation

The pipeline itself is validated via:

- `python -m scripts.wake_word_v3 --plan` — prints the planned job counts and exits without generating.
- `python -m scripts.wake_word_v3 --count` — prints on-disk corpus sizes.
- `python -m scripts.wake_word_v3 --max-base 100 --augment-fraction 0.05` — small dry-run completes in < 5 minutes; useful for verifying environment + Edge TTS rate limits before committing to the full run.

The trained model is validated against:

- **Real production audio** captured via the wake-FP capture path ([[wake-fp-pcm-capture]]) — every false-positive logged since 2026-04-23 is in `data/wake_fp/` and forms a small but real held-out test set.
- **Subjective in-VC test.** Owner says "Hey Poob, skip" / "A Poob, skip" / "Pay Poob, skip" / "Poob, skip" / "Hey Noob, skip" — all five should fire.
- **FP regression suite.** Replay a corpus of conversational utterances containing the phonetic-neighbor words in non-wake context; FP rate should stay < 1/hour.

## Rollback

Operator-level: revert `WAKE_WORD_MODEL_PATH` env var in the Komodo poob stack's Environment panel back to `data/hey_poob.onnx`. Bot restart picks up the old model. No code change required.

Repo-level: revert this commit to remove the v3 pipeline. The v2 model + v2 pipeline are untouched and continue to work.

## Cross-references

- Research foundation: [[wake-word-augmentation-2026]]
- Operational procedure: [[wake-word-retrain-v3]] (runbook)
- Gate semantics that complement the model: [[wake-word-dual-gate]]
- FP-capture data feeding the held-out test set: [[wake-fp-pcm-capture]]
- Existing v2 training pipeline (re-used for feature extraction + training): `scripts/train_hey_poob_v2.py`
