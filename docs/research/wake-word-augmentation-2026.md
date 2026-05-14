---
type: research
status: active
date: 2026-05-13
tags: [voice, wake-word, training, augmentation]
related: [[wake-word-latency]] [[wake-word-dual-gate]] [[wake-fp-pcm-capture]] [[voice-architecture]]
---

# Hey Poob — Wake-Word Augmentation SOTA Survey (2024-2026)

Scope: maximize real-world recall on "Hey Poob" without inflating false-positive rate, for a [openWakeWord](https://github.com/dscripka/openWakeWord) DNN model trained on Whisper-style speech embeddings, deployed in a Discord multi-user voice channel. Current training already uses ACAV100M (~2000 hrs) as background negative features. The dual-gate ([[wake-word-dual-gate]]) already handles music/TTS loopback false positives, so failure modes 1-3 (STT-garbled prefixes, no-prefix wake, phonetic near-miss FPs) are the targets here.

---

## 1. Hard-negative mining

The dominant 2024-2026 finding: **phonetically-confusable hard negatives are the single highest-leverage class of training data after real positives**. The mechanism is curriculum learning — easy negatives (ACAV100M random audio) teach gross discrimination; phonetic confusables teach fine-grained boundary placement.

Concrete approaches:

- **LLM-Synth4KWS (Google, May 2025)** generates hard negatives via vowel-grouped LLM prompting: 20 English vowel phonemes, ~100 simple-but-distinguishable words per vowel, synthesized across 726 speakers × 5 prosodies. Training batch composition: **50% real (MSWC) + 50% synthetic confusables**. Result on Speech Commands: confusable-AUC **0.915 → 0.812 (11.3% relative)**, hard-AUC on LibriPhrase-1s **14.4% → 12.6% (12.5% relative)**. ([arxiv:2505.22995](https://arxiv.org/html/2505.22995v1))
- **OK Aura dataset** explicitly curated phonetic-similarity-tiered hard negatives and forced every batch to include some, demonstrating that *batch-guaranteed* hard negatives outperform proportional sampling. ([Custom Wake Word Detection, Interspeech 2024](https://www.isca-archive.org/interspeech_2024/v24_interspeech.pdf))
- **Adaptive curriculum loss**: weight easy negatives high at training start, ramp up hard-negative weight over training. Reduces convergence time and final FP rate. ([sciencedirect: contrastive curriculum weighting](https://www.sciencedirect.com/science/article/abs/pii/S002002552400447X))
- **Amazon (Towards Data-Efficient Modeling)** found adversarial / phonetically-similar word negatives contribute "significantly" more than equivalent-sized random negative pools. ([amazon.science PDF](https://assets.amazon.science/7c/b2/5e3e6a164920bfc167fb5586d3f2/scipub-1260.pdf))

For "Hey Poob" specifically, the relevant phonetic neighborhoods are:

| Neighborhood | Why it matters | Examples |
|---|---|---|
| /uː/ + /b/ coda | The wake stem ("poob") rhymes with many common words | boob, tube, lube, cube, rube, newb, noob, doob |
| /uː/ + non-/b/ coda | Vowel match, coda confusion | poop, pool, fool, cool, hoop, loop, soup |
| /p/ + /uː/ onset | Onset match | pooh, poop, pool, pooch, poor, push, putt |
| "Hey X" prefix collisions | Hey-X is half the trigger | hey dude, hey you, hey Siri, hey Google, hey Bruce |
| Stop-vowel-stop CVC siblings | Acoustic envelope match | bub, pub, cup, cub, stoop, scoop |

The current adversarial list ([scripts/train_wake_word.md](../../scripts/train_wake_word.md)) covers most "hey X" collisions but is light on bare-stem confusables and on the /p-u-b/ vs /p-u-p/ vs /b-u-b/ minimal triplet. **Add bare "boob", "noob", "tube", "pub", "newb", "poop", "stoop" (without the "hey" prefix) — bare confusables are critical for failure mode 2 (no-prefix wake).**

## 2. Positive-class augmentation — empirical sweet spots

| Augmentation | Empirical sweet spot | Source | Notes |
|---|---|---|---|
| Speed perturbation | **±10% (0.9, 1.0, 1.1)** is the Kaldi-canonical 3-way recipe; ±20% degrades on KWS. | [Speed-Robust KWS, IEEE 2023](https://ieeexplore.ieee.org/document/10023254/), Kaldi recipe convention | Beyond ±15% you hurt vowel formants enough to inflate FPs on bare-stem variants. |
| Pitch shift | **±2 to ±3 semitones** (openWakeWord default is -3 to +3, prob 0.25). | [openwakeword/data.py](https://github.com/dscripka/openWakeWord) | ±4 fine for diversity; ±6 starts producing acoustically implausible speakers. |
| Gain / volume | **-6 to +6 dB** (openWakeWord SevenBandParametricEQ default). | [openwakeword/data.py](https://github.com/dscripka/openWakeWord) | Cheap, applied at 100% probability in OWW. |
| Background noise (SNR distribution) | **OWW default: -10 to +15 dB at 75% probability.** Bias toward the noisy end (0-10 dB) for multi-user Discord. | [openwakeword/data.py](https://github.com/dscripka/openWakeWord); [Home Assistant wake-word approach](https://www.home-assistant.io/voice_control/about_wake_word/) | Discord voice channels live in roughly 5-15 dB SNR; train heavier in that band. |
| RIR / reverberation | OWW applies RIR at **50% probability**. RT60 0.3-1.2 s reduces FRR by 5.6-18.3% across environments. | [openwakeword/data.py](https://github.com/dscripka/openWakeWord); openWakeWord README | Use MIT IR survey + RWCP + AIR + simulated RIRs from MUSAN. [OpenSLR 28](https://www.openslr.org/28/) is the canonical pack. |
| Onset/offset clipping | **Not in openWakeWord's `augment_clips()`.** Must pre-augment. | [openwakeword/data.py](https://github.com/dscripka/openWakeWord) | High leverage for the STT-garbled-prefix failure mode — see §3 below. |
| TanhDistortion | 0.0001-0.10 at 25%. | [openwakeword/data.py](https://github.com/dscripka/openWakeWord) | Simulates mic clipping / codec distortion. Helps with Discord Opus artifacts. |
| BandStopFilter | 25% probability. | [openwakeword/data.py](https://github.com/dscripka/openWakeWord) | Cheap, helps with EQ-shaped audio paths. |
| SpecAugment (time/freq mask) | **Not in openWakeWord.** Adds ~5% relative WER improvement when *added on top of* speed perturbation in ASR; for tiny KWS models the gain is smaller. | [Contrastive Augmentation for KWS, arxiv:2409.00356](https://arxiv.org/html/2409.00356v1) | Worth adding if training plateaus, otherwise skip. |
| Voice-cloning TTS expansion | High leverage for low-resource personal wake words — see §4 below. | [Synth4Kws, arxiv:2407.16840](https://arxiv.org/html/2407.16840) | Diminishing returns past ~1.9M synthetic utterances. |

## 3. Phonetic variant training for "Hey Poob"

The current positives ([scripts/generate_wake_word_samples.py](../../scripts/generate_wake_word_samples.py)) cover "Hey Poob", "Hey, Poob", "Ay Poob", "Ey Poob" — but not the documented failure modes ("A Poob", "Pay Poob", "Hi Poob", "Yo Poob", "Eh Poob", bare "Poob").

**Recommendation: train on a multi-prefix positive set, with explicit weighting.**

| Variant class | Weight | Rationale |
|---|---|---|
| "Hey Poob" canonical | 5× | Primary wake phrase; oversample. |
| "Hey, Poob"; "Hey Poob, X" | 2× | Already covered well. |
| "Hi Poob", "Yo Poob", "Eh Poob", "Ay Poob", "Ey Poob" | 1× each | Casual prefix variants seen in production logs. |
| "A Poob", "Pay Poob" (STT-garbled lookalikes) | 0.5× each | These are *acoustic* near-misses of "Hey Poob" — the audio detector hears the same envelope STT confuses; training on these directly hardens recall. |
| Bare "Poob" (no prefix) | **1×** but carefully | Required for failure mode 2 (user drops "Hey"). **Risk: inflates FPs on conversational "boob" / "noob" / "pub"** — must be paired with heavy bare-confusable hard negatives (§1). |

There is no published study confirming bare-stem inclusion *for an English wake word* helps the full-phrase recall; the evidence from ASR-side biasing (Deepgram keyterms, Picovoice transfer learning) suggests it's neutral-to-positive on recall and slightly negative on FP rate. **Train two variant models if budget allows: (a) full-phrase only, (b) full-phrase + bare-stem with 4× hard-negative weight on bare confusables.** A/B their FP/hour in production.

Picovoice's official guidance: best wake words have **≥6 phonemes, mixed vowel sounds, and are phonetically distinct from common speech**. ([picovoice.ai FAQ](https://picovoice.ai/docs/faq/porcupine/)) "Poob" is 3 phonemes — Porcupine explicitly recommends against it. openWakeWord/MatchboxNet handle short triggers better than Porcupine's transfer-learning approach, but the warning is real: bare "Poob" will have a structurally lower FP ceiling than "Hey Poob".

## 4. TTS-generated positives

The strongest 2024-2025 finding from Google's Synth4Kws is that **TTS diversity, not raw volume, drives improvements**.

- **Engine diversity > sample count.** Adversarial training [arxiv:2408.10463](https://arxiv.org/html/2408.10463) confirms TTS-overfit is real — train on one TTS, generalize to that TTS. Mix Piper, Edge, Coqui XTTS-v2, ElevenLabs free tier, Google Chirp3 if quota allows. Each engine has a distinct prosody/formant fingerprint; stacking ≥3 reduces overfitting.
- **Speaker count matters more than utterance count past ~50 utterances per speaker.** Synth4Kws used 726 speakers × 5 prosodies × 38k phrases = 1.9M utterances. The ablation shows diminishing returns past ~50 utterances per phrase per speaker.
- **Prosody control flags matter.** Piper exposes `--noise-scale` and `--noise-scale-w` for timing/style jitter ([ESPHome microWakeWord](https://github.com/esphome/micro-wake-word-models)); use them. Edge TTS exposes rate/pitch SSML.
- **Synthetic-to-real ratio in low-resource settings: ~38:1** is the optimal point Synth4Kws found at 50k real baseline. For Poob with ~0 real positives, push 100% synthetic and rely on diversity. If you can collect even 20-50 real recordings of "Hey Poob" from the actual user, the EER floor drops fast — [Synth4Kws fig 4](https://arxiv.org/html/2407.16840) shows roughly 700k real utterances are needed to hit 5% EER from real-only data, but a few hundred real samples *combined with* TTS data closes most of the gap.
- **Bias positive synthesis toward production demographic.** If the bot is used by one operator + a small Discord group, voice-clone-augment from their actual recordings (XTTS-v2, OpenVoice, F5-TTS) — every wake utterance the operator says in production is essentially trained-on.
- **Background-mix at the synthesis stage, not just at the training stage.** openWakeWord already mixes positives with background at training; doing it *also* at the synth step (pre-train) decorrelates the noise mask the model learns. (Anecdotal — no published ablation.)

## 5. Architecture: is the openWakeWord DNN the right shape?

For the "tiny custom wake word, ~100-1000 positives" regime:

- **openWakeWord's DNN (1 block, 128 dim)** is intentionally small (~70k params); designed to overfit gracefully on synthetic + ACAV100M. It's the right starting point and is what every published OWW recipe and microWakeWord ship today.
- **MatchboxNet (1D time-channel-separable CNN, ~93k params)** has SOTA accuracy on Google Speech Commands V2 with fewer parameters than ResNet. Conformer-based KWS slightly edges MatchboxNet on continuous-audio tasks. ([arxiv:2506.11169 small-footprint KWS review](https://arxiv.org/html/2506.11169v1)) Neither is integrated with openWakeWord's training pipeline today.
- **Realistic upgrade path**: stay on openWakeWord's DNN until **all augmentation levers in §1-§4 are exhausted**. If recall@FP=1/hr is still <90% after that, the bottleneck is the architecture; consider porting to microWakeWord (which uses Google Research's streaming inception net and is ESPHome-deployed today).

Don't switch architectures to fix a data problem. The Synth4Kws and LLM-Synth4KWS papers explicitly show data-side improvements dominate architecture-side improvements at this scale.

## 6. Threshold + post-processing tuning (highest-leverage)

This is the **largest single dial that doesn't need retraining**.

- openWakeWord runs at 80 ms frames; default trigger averages the last 3 frames (240 ms window). Production deployments at Rhasspy use **activation_threshold=0.7, deactivation_threshold=0.2** with a 3-frame moving average. ([Rhasspy community](https://community.rhasspy.org/t/openwakeword-new-library-and-pre-trained-models-for-wakeword-and-phrase-detection/4162))
- **Per-user calibration**: in Discord we know who's speaking (per-user PCM stream). Maintain a per-user score histogram over a rolling 24h window; set the per-user threshold at the 99th percentile of *that user's* non-wake distribution. This is the single best post-model tweak — it adapts to mic quality, accent, and ambient noise per speaker for free.
- **Temporal aggregation**: require **N=2 of 3 frames above activation_threshold within 240 ms**, not just the mean — this is more conservative and roughly halves FPs from spike noise (Apple's published Hey Siri practice; [picovoice 2026 guide](https://picovoice.ai/blog/complete-guide-to-wake-word/)).
- **Cooldown**: after a positive trigger, suppress further triggers for 1.5-2 s. Already standard in OWW.
- **Dual-gate (current Poob design)** is the correct shape; tune its STT-side fuzzy regex to *narrow* now that the acoustic stage is more reliable.

## 7. Combined positive prefix expansion — one model or several?

Three viable structures:

1. **One big mixed model** (current plan). Easy to deploy, single threshold to tune. Best if the positive prefix variants are acoustically related enough that one decision boundary handles them. Risk: bare "Poob" inflates FPs.
2. **Two-model ensemble**: Model A = "Hey Poob" full-phrase strict; Model B = bare "Poob" with elevated bare-confusable hard-negative weight. OR them together. Higher CPU, but lets each model have its own optimal threshold. **This is the recommended shape if production logs show ≥20% of intents are no-prefix.**
3. **Hierarchical**: cheap MFCC-energy gate → "is this a 'P-u-b'-ish onset?" detector → full DNN. Overkill for this scale.

Recommendation: **start with #1; if FPs spike from the bare-stem addition, fork to #2** rather than tuning a single model to two conflicting objectives.

## 8. openWakeWord-specific: what `augment_clips()` does and doesn't do

From [openwakeword/data.py](https://github.com/dscripka/openWakeWord) head of main as of 2026:

**Built-in (`augment_clips()`):**
- SevenBandParametricEQ (-6 to +6 dB, p=0.25)
- TanhDistortion (0.0001-0.10, p=0.25)
- PitchShift (±3 semitones at 16kHz, p=0.25)
- BandStopFilter (p=0.25)
- AddColoredNoise (SNR 10-30 dB, decay -1 to +2, p=0.25)
- AddBackgroundNoise (SNR -10 to +15 dB, p=0.75)
- Gain (max 0 dB, p=1.0)
- RIR convolution (p=0.5)

**Must add manually (pre-augmentation):**
- Speed/time perturbation (Kaldi-style ±10%)
- SpecAugment (time + frequency masking)
- **Onset/offset clipping** — chop the first 60-200 ms of "Hey Poob" recordings to simulate VAD truncation; this is the direct fix for failure mode 1 (STT-garbled prefix audio). Trivial to script.
- Multi-TTS-engine synthesis diversity (Piper alone is fine for OWW's official recipe but limits prosodic diversity)

Practical: pre-augment the WAVs with speed/onset/specaug *before* feeding to openWakeWord's `compute_features_from_clips()`, then let `augment_clips()` layer on noise/RIR/EQ.

---

## Top 5 highest-leverage augmentation recipes (ranked by expected recall lift per hour of work)

1. **Add onset-clipped positives (60-200 ms head trim)** — script: take every "Hey Poob" WAV, generate 3-5 clipped variants per file, label as positive. Directly targets failure mode 1 (STT-garbled prefix). **Effort: 1 hr. Expected lift: 10-20% relative recall on prefix-garbled variants.**
2. **Expand bare-stem hard-negative set** — add bare "boob", "noob", "tube", "pub", "newb", "poop", "stoop", "doob", "loop", "soup" without the "hey" prefix, 100+ TTS samples each across 3+ engines. Required if adding bare "Poob" as a positive. **Effort: 2 hrs. Expected lift: cuts FP/hr by 30-50% on bare-confusable triggers.**
3. **Add 4-5 multi-prefix positive variants** — "Hi Poob", "Yo Poob", "A Poob", "Pay Poob", "Eh Poob" — 200+ TTS samples each via Edge + Piper + Coqui. Targets failure mode 1 directly. **Effort: 2 hrs. Expected lift: 5-15% relative recall on alt-prefix variants.**
4. **Per-user threshold calibration** — maintain rolling 24h score histograms per Discord user, set threshold at the per-user 99th percentile. No retraining required. **Effort: 4 hrs of dual_pipeline.py work. Expected lift: 20-40% FP reduction with no recall cost.**
5. **Speed perturbation ±10% (3-way Kaldi recipe) + multi-TTS-engine positive synthesis** — re-generate positives via Edge + Piper + Coqui XTTS-v2 (voice-cloned from existing samples) at 0.9/1.0/1.1 speed. **Effort: 4-6 hrs (mostly compute). Expected lift: 5-10% relative recall, mostly on accent/rate edge cases.**

## Don't bother (low ROI on a small custom wake word)

- **SpecAugment time/frequency masking** beyond what openWakeWord already does indirectly via BandStopFilter — adds complexity for ~2% lift at this scale.
- **Pitch shift beyond ±3 semitones** — produces voices that don't exist in production, inflates FPs.
- **Speed perturbation ±20% or ±30%** — degrades vowel formants enough to leak into bare-stem confusables.
- **Architecture swap to Conformer / MatchboxNet before exhausting data-side augmentation** — Synth4Kws and LLM-Synth4KWS both show data dominates architecture at this scale. Spend the engineering on data first.
- **GAN-style adversarial audio augmentation** (e.g. Maximum-Entropy Adversarial Augmentation, [arxiv:2401.06897](https://arxiv.org/html/2401.06897)) — published lifts are 1-3% at the cost of 10× training complexity. Skip unless we're shipping a product.
- **Custom verifier model (the OWW second-stage post-filter)** — useful for *high-FP* deployments; the dual-gate ([[wake-word-dual-gate]]) already serves this role for Poob. Adding another verifier on top is duplicate effort.
- **Training on 24kHz / 48kHz** — openWakeWord features assume 16kHz; resampling Discord 48kHz down is mandatory, not optional, and there is no quality gain from running the features at higher SR.

---

## Sources

- [openWakeWord repo + `augment_clips` source](https://github.com/dscripka/openWakeWord)
- [openWakeWord automatic_model_training notebook](https://github.com/dscripka/openWakeWord/blob/main/notebooks/automatic_model_training.ipynb)
- [Synth4Kws — Google, July 2024 (arxiv:2407.16840)](https://arxiv.org/html/2407.16840)
- [LLM-Synth4KWS — May 2025 (arxiv:2505.22995)](https://arxiv.org/html/2505.22995v1)
- [Training Wake Word Detection with Synthesized Speech Data on Confusion Words (arxiv:2011.01460)](https://arxiv.org/abs/2011.01460)
- [Adversarial training of KWS to minimize TTS overfitting (arxiv:2408.10463)](https://arxiv.org/html/2408.10463)
- [Custom Wake Word Detection — OK Aura, Interspeech 2024](https://www.isca-archive.org/interspeech_2024/v24_interspeech.pdf)
- [Towards Data-Efficient Modeling for Wake Word Spotting — Amazon](https://assets.amazon.science/7c/b2/5e3e6a164920bfc167fb5586d3f2/scipub-1260.pdf)
- [Small-footprint KWS comprehensive review (arxiv:2506.11169)](https://arxiv.org/html/2506.11169v1)
- [MatchboxNet (arxiv:2004.08531)](https://arxiv.org/pdf/2004.08531)
- [Contrastive Augmentation for KWS (arxiv:2409.00356)](https://arxiv.org/html/2409.00356v1)
- [Maximum-Entropy Adversarial Audio Augmentation for KWS (arxiv:2401.06897)](https://arxiv.org/html/2401.06897)
- [Speed-Robust KWS via Soft Self-Attention](https://ieeexplore.ieee.org/document/10023254/)
- [ESPHome microWakeWord training pipeline](https://github.com/esphome/micro-wake-word-models)
- [Picovoice 2026 wake-word guide](https://picovoice.ai/blog/complete-guide-to-wake-word/)
- [Home Assistant wake-word approach](https://www.home-assistant.io/voice_control/about_wake_word/)
- [OpenSLR 28 — RIR + noise database](https://www.openslr.org/28/)
- [Rhasspy openWakeWord deployment](https://community.rhasspy.org/t/openwakeword-new-library-and-pre-trained-models-for-wakeword-and-phrase-detection/4162)
