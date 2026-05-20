---
type: plan
status: active
date: 2026-05-16
tags: [voice, wake-word, training, openwakeword, follow-up]
related: [[wake-word-v3-shipped]] [[wake-word-mass-augmentation-v3]] [[wake-word-retrain-v3]]
---

# Wake-word v4 plan — close the phonetic-neighbor + bare-stem gaps from v3

## Goal

Extend v3's coverage so the audio-side wake gate fires on the phonetic-neighbor positives v3 currently rejects:

- `Hey Noob` (v3 scores 0.0014-0.0021 — rejected)
- `Hey Tube` (v3 scores 0.0049-0.068 — rejected)
- `Hey Rub` / `Hey Lube` (v3 rejects, same vowel-shift pattern)
- `Hey Boob` (v3 is borderline — 2 of 3 samples fire)
- Bare-stem `Poob` / `A Poob` with no trailing context (v3 score ~0.175 — borderline)

The user explicitly requested phonetic-neighbor coverage as part of "anything that could possibly be Hey Poob." v3 generalized to `Hey Poop / Pub / Pube` (second-word p-initials) but stopped at the broader `Hey _oob` envelope.

## Hypothesis

v3 learned the discriminative wedge is the `poob/poop/pub` phoneme cluster — a second-word initial `p`. Phonetic neighbors with `n/t/r/l` initials look acoustically distinct enough that the model classified them as different words. The bare-stem case (just `Poob` with no continuation) sits near the decision boundary because most training positives carry a trailing context that gives the embedder a richer signal.

Root cause is class-balance in the corpus: 449 phrase variants × 47 voices × 8 rates = 168,824 max combinations, but after sanitization-collisions only 112,800 unique positive files landed on disk. Within those 112k positives, the `hey_<phonetic-neighbor>` phrases (49 phrases in the `phonetic_neighbors` slice per `phrases.py`) are a minority — roughly 11% of unique positives. The model picked the easier `_poob` decision boundary because it covered 89% of positive mass.

## Approach

Three changes, in priority order:

1. **Weight phonetic-neighbor phrases 3-5× heavier in the corpus.** The cleanest way is to repeat them at multiple "slurred" / "drawled" tags in the phrase list so they have more unique sanitized filenames and survive dedup. Concretely: add intentional duplicates of each phonetic-neighbor phrase with explicit prefix/suffix punctuation variants (e.g. `Hey Noob.`, `Hey Noob,`, `Hey Noob!`, `Hey, Noob`, `Hey... Noob`) that sanitize to different output filenames. Target: ~5× current phonetic-neighbor mass without inflating canonical-`Poob` mass.

2. **Add bare-stem positives at higher count.** v3 includes bare `Poob` and `A Poob` (no trailing context) but they collapse to small file counts after dedup. Same fix — punctuation variants give distinct on-disk filenames. Target: bare-stem positives should be ~5% of corpus, currently sub-1%.

3. **Add a per-phrase weight column to the orchestrator** if (1) and (2) don't move the needle. The orchestrator currently treats every phrase × voice × rate combination equally. A small additional change to `_build_jobs` could replicate phonetic-neighbor jobs N times into the queue (each writing to a unique filename via an index suffix). Cost: code change in [orchestrator.py](../../scripts/wake_word_v3/orchestrator.py) plus a `--phrase-weight` flag in the CLI.

## Cost

Time:

- Phrase-list edits: ~30 min (no code change, just `phrases.py`)
- Corpus regen (delta): the phonetic-neighbor + bare-stem positives that don't exist yet → a few thousand TTS jobs → ~30-60 min Edge TTS
- Pitch + augmentation pass: ~1 hour
- Feature extraction (delta): only new positives extracted (~50k samples) → ~30 min CPU
- Training: 10 min (50k steps on CPU is fast once features exist)
- Inference test: ~5 min

Total: ~3-4 hours wall-clock, mostly TTS + augmentation. No GPU acceleration needed.

Risk:

- Higher false-positive rate on real-world "noob" / "tube" speech (e.g. someone using "noob" as gaming slang). Mitigated by adversarial-negative coverage — add `you noob`, `complete noob`, `tube of toothpaste`, `loop in the road` already in v3's neg corpus; that stays the same. The v3 neg corpus already includes these patterns and they reject cleanly.
- Possibly slight regression on canonical `Hey Poob` if the model spreads its decision boundary too thin. Mitigation: hold-out test against v3-passing samples post-v4 train.

## Files likely to change

- [scripts/wake_word_v3/phrases.py](../../scripts/wake_word_v3/phrases.py) — add the phonetic-neighbor and bare-stem variants with intentional punctuation diversity.
- (Optional, if approach 3 needed) [scripts/wake_word_v3/orchestrator.py](../../scripts/wake_word_v3/orchestrator.py) + [scripts/wake_word_v3/__main__.py](../../scripts/wake_word_v3/__main__.py) for per-phrase weight CLI.
- [scripts/wake_word_v3/train_inside_container.py](../../scripts/wake_word_v3/train_inside_container.py) — bump `val_steps` from default `[250]` to something like `[1000, 5000, 10000, 25000, 50000]` so we get useful intermediate metrics (this would also have helped diagnose v3 faster).
- `data/hey_poob_v4.onnx` (new) — the v4 model.
- [docs/decisions/wake-word-v3-shipped.md](../decisions/wake-word-v3-shipped.md) — supersede with a v4-shipped decision note once landed.

## When

Not blocking. v3 ships first, gets a live test in voice channel against real speech. If a user genuinely says "Hey Noob" or "Hey Tube" in production and it gets missed, that's the trigger to start v4. Otherwise dual-gate text fallback ([[wake-word-dual-gate]]) covers these cases via STT pattern matching as long as the user includes trailing context.
