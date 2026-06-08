---
type: decision
status: active
date: 2026-06-08
tags: [brain, llm, routing, resilience, rate-limit, groq]
related: [[groq-failfast-client]] [[groq-daily-cap-routing-storm]] [[gemini-tool-router-rung]]
---

# Provider circuit breaker — skip a rate-limited model for its advised window

## Decision

When a model in the tool-routing cascade returns a 429, the brain records a
per-model **cooldown** for the window the 429 itself advises, and the cascade
**skips that model** until the window passes. This converts the recurring
"re-probe the capped model on every turn" waste into "route straight to the
next healthy rung (Gemini) until the server says Groq is back."

## Why now (it was deferred — the data arrived)

This was explicitly deferred pending measurement of whether the residual
re-probe cost mattered. The 2026-06-08 routing storm ([[groq-daily-cap-routing-storm]])
settled it: **142 × 429 / 130 cascade fall-throughs** in one session, with
23-second first-word latency spikes, because the cascade re-probed Groq's
exhausted 200k-TPD model on every back-to-back question. The 429 response
*itself* carries the exact retry time (`Please try again in 25m7.68s`), so the
breaker is driven by the server's signal — **not a guessed TTL**, which was the
operator's stated bar for doing this the right way.

## How

In `PoobBrain` (`src/poob/brain/poob.py`):

- `_provider_cooldown: dict[model -> monotonic deadline]`. Keyed by **model**,
  not provider — Groq's TPD is per-model, so Scout (`llama-4-scout`) keeps
  routing when `gpt-oss-20b` is capped.
- `_is_rate_limit_error` — only 429 / rate-limit / quota errors arm the breaker.
  **Timeouts and other failures do NOT** (those are transient; re-probe is fine).
- `_retry_after_seconds` — prefers the `Retry-After` header, falls back to the
  `"try again in 2m5.3s"` message regex. Clamped to [5s, 30min]; a rate-limit
  with no advised time uses a 60s default.
- `_active_providers(providers)` — drops cooled models before the cascade loop;
  **never strands** (if every model is cooling, returns the full list).
- The loop **clears** a model's cooldown on its first success (half-open →
  closed), and **arms** it in the `except` path via `_note_model_rate_limited`.

Composes with the fail-fast client ([[groq-failfast-client]]): fail-fast makes
the occasional probe cheap (~100ms), the breaker removes the probe entirely
during a known cap window.

## Scope / non-goals

- Applies to the **tool-routing cascade** only. The casual/streaming Groq paths
  (the other client sites) call Groq directly and don't yet consult the
  cooldown — that's the same follow-up as extending the fail-fast factory to
  those sites. They share `_provider_cooldown` state when we get there.
- Not a token-budget tracker — it reacts to 429s, it doesn't predict the cap.
  Raising/extending the ceiling (2nd Groq key, prompt-token trim, Gemini-primary
  at peak) is a separate open decision.

## Validation

- `tests/unit/test_provider_circuit_breaker.py` (15): retry-after parsing
  (header + message, fractional min/sec), rate-limit-vs-timeout classification,
  cooldown set/expiry/clamp, per-model skip (Scout survives), never-strand,
  no-cooldown-when-none.
- Full unit suite green.
- Post-deploy telemetry: `provider.cooldown_set` / `provider.cooldown_skip`
  during cap windows; `poob.tool_route provider=gemini` carrying load; the 23s
  latency spikes gone.
