---
type: decision
status: active
date: 2026-09-08
tags: [resilience, rate-limit, llm, stt, routing, cascade]
supersedes: [[provider-circuit-breaker]]
related: [[groq-daily-cap-routing-storm]] [[cascade-outage-nvidia-hang-gemini-rpm]] [[gemini-tool-router-rung]] [[stt-cooldown-shipped-inert]] [[routing-tpm-ceiling-degrades-cascade]]
---

# Provider cooldown is now a shared, provider-agnostic registry — not brain-only logic

## Decision

The rate-limit circuit breaker [[provider-circuit-breaker]] built for the LLM
tool-routing cascade is extracted into `poob.resilience.provider_cooldown.
ProviderCooldownRegistry` — a standalone class with no dependency on
`PoobBrain`, Groq, or Gemini specifically. Any cascade that tries a list of
candidates in order can use it: pass a list, a `key` function extracting a
cooldown key per item, and it returns the subset not currently cooling
(falling back to the full list if everyone is cooling, so a cascade never
strands with zero candidates).

`PoobBrain` now holds one `ProviderCooldownRegistry` instance
(`self._provider_breaker`) instead of owning the logic itself, and other
subsystems reach into that same instance so a rate-limited model is skipped
**everywhere in the process**, not just in the routing cascade — closing the
gap [[provider-circuit-breaker]] explicitly flagged as scope-limited
("Applies to the tool-routing cascade only... share `_provider_cooldown`
state when we get there").

## Why now

Requested directly: *"when a model runs out of credits, for that amount of
time we must omit calling those previous models and continue calling the
next cascaded model, gracefully... when the models start relieving their
rate limits, we go RIGHT BACK to the previous one... implement this system
robustly so that we can wire in any llms from any providers."*

The immediate trigger was [[stt-cooldown-shipped-inert]]: a same-day PR
attempted exactly this for the STT cascade, declared an empty
`_stt_provider_cooldown` dict and a new exception type, but never wired a
check or a set anywhere — the existing generic `except Exception` handler
already produced identical behavior with or without the change. Zero tests
shipped with it. Rather than hand-roll a second, STT-specific cooldown
(which would have re-earned the exact same class of bug independently),
this extracts the ALREADY-PROVEN mechanism and reuses it.

## What moved, and what didn't

Pure extraction of `PoobBrain`'s `_is_rate_limit_error`,
`_retry_after_seconds`, `_is_timeout_error`, `_model_in_cooldown`,
`_note_model_rate_limited`, `_note_model_timeout`, `_active_providers` — byte
identical logic, same constants (5s floor / 1800s ceiling / 60s default
cooldown; 2 consecutive timeouts arm a 120s cooldown), now configurable via
the registry's constructor instead of hardcoded class attributes (the "any
provider, any config" requirement — a future caller with different
thresholds doesn't need to fork the class).

`PoobBrain` keeps the SAME public surface for backward compatibility:
`_provider_cooldown` and `_provider_timeouts` are now properties returning
the registry's internal dicts directly (same mutable objects, not copies),
and the original method names delegate to the registry while preserving the
brain's own telemetry event names (`provider.cooldown_set`,
`provider.cooldown_skip`) — a generic resilience module has no business
emitting brain-specific log events, so that logging stayed at the call site.
All 20 of `provider-circuit-breaker`'s original tests pass unmodified
against the refactored `PoobBrain`, proving this is a behavior-preserving
extraction and not a rewrite.

## How the STT cascade now uses it

`VoiceSession._transcribe` / `_transcribe_16k` (`src/poob/voice/session.py`)
reach into `self.brain._provider_breaker` — the SAME instance the routing
cascade uses — and:

1. `breaker.filter_active(self.stt_providers, key=lambda p: p.name)` before
   the loop, so a cooling provider isn't even attempted.
2. `breaker.note_success(provider.name)` on any response (even an empty
   transcript — the provider answered fine, only silence was detected).
3. `breaker.note_rate_limited(...)` + `breaker.note_timeout(...)` in the
   `except` — no-ops for anything that isn't the matching error class,
   mirroring the routing cascade's pattern exactly.

Keys are namespaced by the STT provider's own `.name` property (e.g.
`"gemini_stt:gemini-2.5-flash-lite"`, `"groq_whisper:whisper-large-v3-turbo"`)
so they never collide with the routing cascade's bare model strings (e.g.
`"gemini-2.5-flash-lite"`) even though both share one registry — a genuinely
shared Gemini project-level quota still cools down both correctly since
they're tracked as distinct model identities, which is correct: STT and
chat-completions are typically billed/limited separately per Google's own
API structure, so treating them as separate cooldown keys is the right
granularity, not a workaround.

`STTRateLimitError` (added by the inert PR) is kept — it's still the
mechanism by which a provider signals "this specific failure should cool me
down" versus "this was an ordinary failure, just try the next provider" (no
cooldown for ordinary failures, matching the routing cascade's classification
philosophy of only reacting to signals that mean "definitely still capped
right now").

## Scope / non-goals

- STT timeout-cooldown (mirroring the LLM cascade's 2-consecutive-timeouts
  rule) is NOT wired yet — `DeepgramSTT`'s own generic `except Exception`
  swallows timeouts internally rather than re-raising them, unlike its
  429-detection which does re-raise. Extending timeout-cooldown to STT would
  need each provider to also distinguish and re-raise timeout-shaped
  exceptions, the same way they already do for 429s. Deferred — this pass is
  scoped to the requested "ran out of credits" case, not hangs.
- TTS providers (`_synthesize` cascade, same file) do not yet consult the
  registry. Same shape of follow-up as [[provider-circuit-breaker]]'s
  original "casual/streaming Groq paths... that's the same follow-up"
  scope note — noted rather than silently left undone.
- This is a rate-limit backstop, not a token-budget predictor — it reacts to
  429s after they happen, same as before. Reducing routing's per-call token
  cost is the separate, already-tracked problem in
  [[routing-tpm-ceiling-degrades-cascade]].

## Validation

- `tests/unit/test_provider_cooldown_registry.py` (22): retry-after parsing,
  classification, cooldown set/expiry/clamp, timeout arming, `note_success`
  clearing both maps, generic `filter_active` over tuples AND over bare
  strings (proving genuine provider-agnosticism), constructor-configurable
  thresholds.
- `tests/unit/test_provider_circuit_breaker.py` (20, pre-existing, unchanged):
  all still pass against the refactored `PoobBrain` — proves the extraction
  is behavior-preserving.
- `tests/unit/test_stt_cascade_cooldown.py` (5, new): the specific gap the
  inert PR left — a rate-limited STT provider is skipped on the very next
  call, rejoins the cascade once its cooldown expires (verified via directly
  simulating deadline expiry rather than a real multi-second sleep),
  `_transcribe` and `_transcribe_16k` share cooldown state, an ordinary
  (non-429) failure does NOT trigger a cooldown, and the cascade never
  strands when every provider is cooling.
- Mutation-verified: reverted `_transcribe` to the exact byte-for-byte
  pattern the inert PR shipped (generic catch, no registry consultation),
  confirmed the two tests targeting that specific gap failed, restored,
  confirmed all 5 pass again.
- Full unit suite: 2004 passed, 1 skipped, no regressions.
