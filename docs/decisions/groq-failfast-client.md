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

## Scope (deliberately narrow)

Applied **only to the routing cascade** (`_call_provider_with_tools`), which has
full downstream failover. The other **7** `AsyncGroq(...)` sites in
[poob.py](../../src/poob/brain/poob.py) — `respond_streaming`, `_groq_stream`,
`_stream_toob_wrap_from_query`, `_stream_boob_wrap_from_query` (streaming) and
`_casual_text_fallback`, `_wrap_music_response`, `_wrap_in_personality`
(non-streaming) — **still use SDK defaults** and are a documented follow-up.
They need per-site care: a tight *total* timeout on a `stream=True` call would
truncate a long spoken reply mid-sentence; streaming sites must instead use an
`httpx.Timeout` with a generous *read* (per-chunk) timeout. The factory is the
seam to extend cleanly once that analysis is done.

## Deferred / follow-ups

- **Circuit breaker / cooldown (the "skip Groq while it's capped" optimization)
  — DEFERRED pending measurement.** After fail-fast, the residual cost under a
  daily-cap is one ~100ms Groq probe per request. At this request volume
  (~dozens of tool-routes/day) that may not be worth the added state and the
  "stuck-open" failure mode (stranding us off the free/fast primary after it
  recovers). If built later, drive the half-open timing off Groq's
  **`Retry-After` / `x-ratelimit-reset-*` headers** (RFC 9110), NOT a guessed
  TTL.
- **Extend the fail-fast factory to the other 7 Groq sites** with
  streaming-safe timeouts (kills the 60s-hang on the casual/wrap paths too).
- **Downstream REST timeout**: the `httpx.AsyncClient(timeout=15.0)` shared by
  the Gemini / NVIDIA / Cerebras branches ([poob.py](../../src/poob/brain/poob.py)
  ~`_call_provider_with_tools`) is also worth tuning, separately.

## Validation

- `tests/unit/test_groq_client_policy.py` — factory pins `max_retries=0`,
  caps the timeout, timeout is required (no silent 60s default), and the routing
  branch constructs its client via the factory.
- Brain + voice unit tests green (78 passed across gemini-router / casual /
  dual-pipeline / wake-matcher); new policy tests 4/4.
