---
type: incident
status: resolved
date: 2026-06-08
tags: [brain, llm, groq, rate-limit, latency, voice, routing]
related: [[groq-failfast-client]] [[provider-circuit-breaker]] [[gemini-tool-router-rung]] [[groq-daily-token-cap-degrades-routing]]
---

# Groq daily-cap (200k TPD) exhaustion → routing storm + 23s voice latency

## Symptom

Operator, on the 2026-06-08 voice session: *"there was a point last night where
we started asking Poob questions back to back for about 4 straight and it got
really slow and backed up. this cannot happen."* — around the "was slavery good
for the economy" questions.

Log evidence (`voice.log`, 2026-06-08 03:15–03:24):

- Back-to-back addressed questions; first-word latency (`llm_ms`) was wildly
  inconsistent: **23386 / 23294 / 22261 ms** spikes interleaved with ~1.3–1.7s.
- Every addressed turn logged `Tool detection failed, trying next
  provider=groq ... error=429`.
- Mangled repeats ("A poop. Was slavery…", "Peypoob, strictly based…") didn't
  register (wake-gate gap — see below), so the operator re-asked → felt ignored
  *and* slow → "Hey, Poob. Shut the [f…]".

Earlier session (2026-06-03→04) had the same pattern at scale: **142 × 429,
130 cascade fall-throughs** in ~3 hours.

## Root cause

The 429 body is explicit:

```
Rate limit reached for model openai/gpt-oss-20b ... service tier on_demand on
tokens per day (TPD): Limit 200000, Used 199956, Requested 3534.
Please try again in 25m7.68s.
```

Groq's **200k-tokens/day cap** is genuinely exhausted during a busy multi-user
voice night (casual chat + music + many utterances burn tokens fast). Once near
the ceiling, *every* routing request 429s. Two compounding faults made this
user-visible:

1. **No fail-fast** — the Groq client ran SDK defaults (`max_retries=2`, 60s
   timeout), so a 429 was retried with backoff (and the SDK honored part of the
   "try again in 2m" header), producing the **~23s first-word spikes** before
   the cascade reached Gemini.
2. **No cooldown memory** — the cascade rebuilt `providers` every turn starting
   at Groq, so it **re-probed the known-capped model on every single question**
   (130 wasted Groq round-trips), and the responses serialized → "backed up".

The cascade itself worked (Gemini absorbed the overflow; answers *did* come) —
it was the latency, not total failure.

## Fix

Two complementary changes (the 429 even hands us the exact wait time):

1. **Fail-fast Groq client** ([[groq-failfast-client]], `f49761d`):
   `max_retries=0` + tight timeout → a 429 raises immediately, no backoff.
2. **Provider circuit breaker** ([[provider-circuit-breaker]]): on a 429, cool
   the model down for the server-advised window parsed from the 429
   (`_retry_after_seconds`); the cascade's `_active_providers` then **skips that
   model** until the window passes, so Gemini becomes rung 1 and no per-turn
   Groq probe happens. Keyed by model (Scout's separate TPD budget is
   unaffected). Half-open: on the first success after the window, the cooldown
   clears.

Net: the slavery-style back-to-back turns would each answer in ~1.3s via Gemini
with zero Groq tax, instead of 23s + serialization.

## Adjacent gaps (tracked separately)

- **Wake missed the question-form repeats** — the wake matcher ([[wake-address-hey-dropout]])
  catches "poob + COMMAND verb" but not "poob + a QUESTION" ("*was* slavery…"),
  so mangled "A poop / Peypoob" repeats fell through. Extending to question-form
  is a separate, carefully-scoped change (false-positive risk).
- **Token ceiling** — 200k/day is the real constraint. Options: a 2nd Groq key,
  trimming the routing prompt's token size, or making Gemini primary at peak.
  Open decision.

## Validation

- `tests/unit/test_provider_circuit_breaker.py` (15) + `test_groq_client_policy.py` (4).
- Full unit suite green.
- Post-deploy: during a cap window expect `provider.cooldown_set model=openai/gpt-oss-20b`
  once, then `provider.cooldown_skip` on following turns, `poob.tool_route
  provider=gemini` carrying routing, and voice first-word latency staying ~sub-2s
  instead of 20s+ spikes.
