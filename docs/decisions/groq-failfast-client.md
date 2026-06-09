---
type: decision
status: active
date: 2026-06-04
tags: [brain, llm, groq, latency, routing, resilience]
related: [[gemini-tool-router-rung]] [[groq-daily-token-cap-degrades-routing]]
---

# Groq client is fail-fast (max_retries=0 + tight timeout) on the routing cascade

## Decision

The tool-routing Groq client (`_call_provider_with_tools`, the cascade rung 1)
is built through a shared factory `PoobBrain._make_groq_client(timeout=...)`
that pins **`max_retries=0`** and an **8s timeout**, instead of the Groq SDK
defaults.

## Why

The provider cascade (Groq → Gemini → NVIDIA → Scout) is the brain's resilience
layer. Retrying a rate-limited Groq inside a real-time voice turn is the
anti-pattern — it just adds dead air before we fail over to Gemini anyway.

Context7-confirmed SDK defaults (`/groq/groq-python`):
- **`max_retries=2`**, retried on 429 / 408 / 409 / ≥500 / connection errors,
  with exponential backoff → a 429 cost **~1.5–3s of backoff** before raising.
- **`timeout=60s`**, and *timed-out requests are themselves retried 2×* → a hung
  call could block **~3 min**. This is the likely source of the observed
  `llm_ms=60181` spike in the production voice logs.

With `max_retries=0` + 8s timeout, a Groq 429 raises immediately and the cascade
reaches Gemini in **~100ms** (the 429 round-trip) instead of ~2–4s; a hung Groq
caps at 8s instead of 60s+.

**Industry-standard framing:** don't retry a rate-limited dependency when you
have a healthy fallback; fail fast and fail over. This is the cheap, stateless
half of the resilience story.

## Scope

The routing cascade (`_call_provider_with_tools`) builds its client via the
`_make_groq_client(timeout=8.0)` factory. **As of 2026-06-09 the other 7
`AsyncGroq(...)` sites are also fail-fast** — `respond_streaming`,
`_groq_stream`, `_stream_toob_wrap_from_query`, `_stream_boob_wrap_from_query`
(streaming) and `_casual_text_fallback`, `_wrap_music_response`,
`_wrap_in_personality` (non-streaming) now construct
`AsyncGroq(api_key=…, max_retries=0, timeout=12.0)` inline.

Why inline rather than the factory here: each of those methods already does a
local `from groq import AsyncGroq`; routing them through the factory would orphan
7 imports at 3 different indents (lint failure), so adding the two kwargs inline
is the lower-risk, equally-fail-fast change. A plain `timeout=12.0` float is
safe for **both** call styles: for a `stream=True` call httpx applies it as the
*per-chunk read* timeout (12s between tokens never happens in a healthy stream,
so no mid-reply truncation); for a non-streaming call it's a 12s total ceiling,
rarely hit because a 429 now fails in ~100ms via `max_retries=0`. A source-level
test (`test_every_groq_client_in_brain_is_failfast`) guards that no future Groq
client reintroduces the SDK default. This closed the 2026-06-09 casual-path
**13s latency spikes** (a `llama-3.1-8b-instant` 429 retry-backoff while the
routing model's TPD was capped — see [[groq-daily-cap-routing-storm]]).

Still Groq-only (no Gemini fallback) on those casual/wrap paths: a 429 now fails
*fast* to "brain glitched / no commentary" instead of a 13s hang. Giving the
casual path a real Gemini streaming fallback (so a capped turn still answers) is
the remaining follow-up below.

## Deferred / follow-ups

- **Circuit breaker / cooldown — BUILT** (2026-06-08, [[provider-circuit-breaker]]).
  The measurement arrived (142 × 429 in one session); it now skips a capped model
  for the window the 429's own `Retry-After` advises.
- **Extend fail-fast to the other 7 Groq sites — DONE** (2026-06-09, see Scope
  above). Remaining sub-item: give the casual/wrap paths a **Gemini streaming
  fallback** so a capped turn answers via Gemini instead of failing fast to
  "brain glitched". Currently they're Groq-only.
- **Downstream REST timeout**: the `httpx.AsyncClient(timeout=15.0)` shared by
  the Gemini / NVIDIA / Cerebras branches ([poob.py](../../src/poob/brain/poob.py)
  ~`_call_provider_with_tools`) is also worth tuning, separately.

## Validation

- `tests/unit/test_groq_client_policy.py` — factory pins `max_retries=0`,
  caps the timeout, timeout is required (no silent 60s default), and the routing
  branch constructs its client via the factory.
- Brain + voice unit tests green (78 passed across gemini-router / casual /
  dual-pipeline / wake-matcher); new policy tests 4/4.
