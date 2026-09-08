---
type: incident
status: resolved
date: 2026-09-08
tags: [voice, stt, rate-limit, testing, gemini]
related: [[provider-cooldown-registry]] [[provider-circuit-breaker]] [[voice-llm-model-deprecated-and-never-wired]]
---

# "fix(stt): cooldown quota-exhausted STT providers" shipped a dict that was never read or written

## Symptom

Operator: *"gpt5-mini went and deployed a fix that didn't do anything."*

PR #13 (merged 2026-09-08 02:18 UTC) claimed to fix repeated Gemini STT 429
storms. The storm itself was real — confirmed in the 09-07 logs, `Gemini STT
failed: 429 RESOURCE_EXHAUSTED` firing roughly every 30-40s (sometimes 7s
apart) for hours, a hard daily-quota exhaustion where every retry in the
window is guaranteed to fail identically.

## Root cause

Read directly from the diff, no log ambiguity involved:

- Added `self._stt_provider_cooldown: dict[str, float] = {}` to
  `VoiceSession.__init__` — declared once, never read, never written
  anywhere else in the codebase (confirmed by grep: that line is the only
  reference to the name in `src/`).
- Made `GroqWhisperSTT`, `GeminiSTT`, `DeepgramSTT` raise a new
  `STTRateLimitError` on 429 instead of returning `""`.
- But the only two callers, `VoiceSession._transcribe` /
  `_transcribe_16k`, already wrapped every provider call in a generic
  `except Exception: log + try next provider`. `STTRateLimitError` is an
  `Exception` subclass, so it falls into that same catch block and produces
  **byte-identical behavior to before the PR** — log a warning, move to the
  next provider, no cooldown ever consulted or set, ever.

The PR shipped infrastructure (an exception type, an empty dict) without
wiring the actual behavior change: check-cooldown-before-call,
set-cooldown-after-429. Same shape as
[[voice-llm-model-deprecated-and-never-wired]] — a mechanism that exists in
source but is never connected to anything.

**Why it shipped broken:** zero tests were added (`git show --stat -- tests/`
on the commit shows none), and the PR description was empty. There was
nothing to fail and catch it before merge — a test asserting "the same
provider is skipped on the very next call after a 429" would have failed
immediately against the shipped code, since the check it asserts never
existed.

## Fix

Rather than write a second STT-specific cooldown (repeating the exact
mistake independently), extracted the ALREADY-PROVEN mechanism from
`PoobBrain`'s LLM-routing circuit breaker
([[provider-circuit-breaker]]) into a reusable
`poob.resilience.provider_cooldown.ProviderCooldownRegistry`, shared between
the routing cascade and the STT cascade via `self.brain._provider_breaker`.
Full design in [[provider-cooldown-registry]].

Removed the dead `_stt_provider_cooldown` dict and the now-unused `import
time` PR #13 left in `session.py`. Kept `STTRateLimitError` — it's the
correct mechanism for a provider to signal "this needs a cooldown," it was
just never consumed.

## Validation

- `tests/unit/test_stt_cascade_cooldown.py` (5): the actual missing
  behavior, in order — a 429'd provider is skipped on the immediate next
  call; it rejoins the cascade once its advised cooldown expires; the
  primary and salvage (`_transcribe_16k`) cascades share cooldown state;
  an ordinary (non-rate-limit) failure does NOT trigger a cooldown; the
  cascade never strands when every provider is cooling.
- Mutation-verified: reverted `_transcribe` to the exact inert pattern this
  PR shipped, confirmed the two tests targeting that gap failed, restored,
  confirmed all 5 pass.
- Full unit suite: 2004 passed, 1 skipped.

## Follow-ups

- No STT timeout-cooldown yet (only rate-limit) — noted as deliberate scope
  in [[provider-cooldown-registry]].
- General lesson, not new to this vault but reconfirmed: a fix that adds
  state without a test asserting the state is actually consulted will ship
  inert, and the existing catch-all exception handling in this codebase is
  permissive enough that "it compiles and doesn't crash" gives zero signal
  that the fix does anything at all.
