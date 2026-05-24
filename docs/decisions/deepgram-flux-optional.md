---
type: decision
status: active
date: 2026-04-23
tags: [voice, stt, deepgram, latency]
related: [[voice-architecture]] [[voice-pipeline-optimization-prompt]]
---

# Deepgram model is configurable; Flux available as opt-in

## Context

Deepgram shipped a new streaming model called Flux in October 2025. Independent benchmarks (Coval, cited by the April 2026 voice-pipeline research) measure it ~450ms P50 faster than Nova-3 on short commands, with the same keyterm-biasing API. Before this change, our Deepgram model was hardcoded to `nova-3` inside `DeepgramStreamManager`.

## Decision

Plumbed `deepgram_model` as a configurable field:

- **`config.py`**: new `deepgram_model: str = "nova-3"`. Default unchanged.
- **`main.py`**: included in `dual_pipeline_config` dict passed to `VoiceSession`.
- **`session.py`**: read from `dual_pipeline_config.get("deepgram_model", "nova-3")` and forward to `DualPipelineProcessor`.
- **`dual_pipeline.py`**: new `deepgram_model` kwarg on `DualPipelineProcessor.__init__`, forwarded to `DeepgramStreamManager`.

## How to try Flux

Set `DEEPGRAM_MODEL=flux-general-en` in the homelab Komodo stack env and redeploy. No other changes needed. Revert by removing the env var or setting back to `nova-3`.

## Exit criterion

If Flux works on the user's Deepgram plan — no auth / not-available errors in production logs — and subjective STT quality on command words (play / skip / stop / pause / volume / shuffle) stays flat or improves, flip `deepgram_model` default to `flux-general-en` in a follow-up commit.

If Flux errors out or drops keyterm recognition, set the env var back and mark this note `status: superseded` pointing at the Nova-3 default.

## Guard metrics

- Tool-routing correctness (is the right tool called?) must stay flat. If the LLM starts mis-routing because transcripts are different, that's a regression.
- Keyterm recall on `play / skip / stop / pause / volume / shuffle` (subjective — does "skip" still fire skip?).

## Rollback

One env var. Zero code change needed.

## Results

<!-- Filled in after the user tries Flux in prod. -->
