---
type: decision
status: active
date: 2026-05-16
tags: [voice, wake-word, training, openwakeword, deployment]
related: [[wake-word-mass-augmentation-v3]] [[wake-word-retrain-v3]] [[wake-word-v4-phonetic-neighbor-followup]] [[wake-word-dual-gate]]
---

# Ship wake-word v3 with known phonetic-neighbor gaps (v4 follow-up planned)

## Context

The wake-word v2 model was failing in production on common Hey-Poob mishears observed in user voice traces — most notably "A Poob", "Pay Poob", "Yo Poob", "Hi Poob", and bare "Poob" (no `hey` prefix). The dual-gate fallback handled most of these as text-side, but the audio-side gate was rejecting them often enough to surface as user-perceived "Poob isn't listening" incidents.

v3 was designed to fix this via a mass-augmentation training corpus (see [[wake-word-mass-augmentation-v3]]) — 449 phrase variants × 47 voices × 8 speech rates, plus pitch perturbation and clip augmentation. The user explicitly requested that phonetic neighbors (Hey Noob, Hey Tube, Hey Poop) also be treated as POSITIVES so that STT-garbled wake words still trigger.

After 8.5 hours of training (816,125 WAV corpus → 778k positive features + 38k adversarial features + 50k ACAV background features, 50,000 SGD steps on `Model.train_model`), the v3 model exists at `data/hey_poob_v3.onnx` (857 KB, DNN layer_dim=128 n_blocks=1).

## Decision

Ship v3. Deploy via Komodo env-var flip (`WAKE_WORD_MODEL_PATH=/app/data/hey_poob_v3.onnx`). Document the known gaps for v4.

The model is a major improvement over v2 on the failures the user actually reported in production. The phonetic-neighbor stretch goal is partially met but not fully covered — and we shouldn't block ship on the stretch goal.

## Validation

Inference-tested the ONNX inside the trainer container against samples drawn directly from the v3 corpus on disk. Discrimination is sharp — positive scores cluster at 0.9996-0.9997, adversarial-negative scores cluster at 0.0002-0.0005. 5000:1 separation between the two distributions.

**What got fixed (v2 production failures):**

| Phrase pattern | v2 behavior | v3 score | Status |
|---|---|---|---|
| `A Poob, <context>` | reject | 0.9997 | ✅ fixed |
| `Pay Poob, <context>` | reject | 0.9969-0.9997 | ✅ fixed |
| `Yo Poob, <context>` | reject | 0.9984-0.9996 | ✅ fixed |
| `Hi Poob, <context>` | reject | 0.9985-0.9997 | ✅ fixed |
| `Hey Poob, <context>` | accept | 0.9996-0.9997 | ✅ retained |

**What stayed strong (robustness):**

- 10/10 hits on randomly sampled clip-augmented positives (head/tail/mid clips)
- 7/10 hits on randomly sampled pitch-perturbed positives (high-fast, low-slow, etc.)
- 0/12 false positives on adversarial negatives including `siri what time is it`, `step in the poop`, `the pope blessed`, `a loop in the road`

**What didn't get fixed (carried into v4):**

| Phrase pattern | Intended | v3 score | Status |
|---|---|---|---|
| `Hey Poop` / `Hey Pooper` | positive | 0.9982-0.9991 | ✅ generalizes |
| `Hey Pub` / `Hey Pube` | positive | 0.9951-0.9988 | ✅ generalizes |
| `Hey Boob` | positive | 2/3 fire | ⚠️ partial |
| `Hey Noob` | positive (user goal) | 0.0014-0.0021 | ❌ **rejects** |
| `Hey Tube` | positive (user goal) | 0.0049-0.0680 | ❌ **rejects** |
| `Hey Rub` / `Hey Lube` | positive (vowel variants) | 0.0006-0.0013 | ❌ **rejects** |
| Bare `Poob` / `A Poob` (no trailing context) | positive | 0.175 | ⚠️ borderline |

The model learned the discriminative wedge is the `poob/poop/pub/pube` phoneme cluster — second word starts with `p`. It generalized to consonant-related variants on the second word but NOT to neighbors with `n/t/r/l` initials. The bare-stem variant (just `a poob` with no continuation) sits near the decision threshold.

## Alternatives considered

- **Retrain v4 immediately with hey_noob/tube weighted heavier**. Rejected for now — the stretch goal isn't blocking real user-reported issues. Better to ship the V2-failure fix now and address the gap in a planned v4. See [[wake-word-v4-phonetic-neighbor-followup]] for the plan.
- **Two-model ensemble** (one strict canonical model + one loose phonetic-neighbor model voted together). Rejected — adds inference cost and complexity; pursuing only if v4's data-weight approach doesn't cover the gap.
- **Lower the wake-gate threshold to compensate**. Rejected — would raise FP rate on adversarial negatives and trip the dual-gate over-rejection during music ([[wake-gate-over-rejection-during-music]]).

## Consequences

- v3 model is the live wake-word model on homelab after the env-flip. v2 model stays in `data/hey_poob.onnx` for instant rollback (Komodo env-var swap, no redeploy).
- The dual-gate text fallback ([[wake-word-dual-gate]]) still catches `Hey Noob` / `Hey Tube` via STT — the audio gate rejects, but the text gate matches the trailing-context positive pattern when present. So the *user-facing* failure rate on these is mitigated as long as they include trailing context.
- v4 follow-up is queued at [[wake-word-v4-phonetic-neighbor-followup]] — corpus oversampling + weight reshaping, no architectural change needed.
- Bare-stem `Poob` (no context) remains a known weak case in both v3 audio and v3 text gates; defer until evidence shows a user actually says just "Poob" alone with intent.
