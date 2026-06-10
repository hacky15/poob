---
type: incident
status: resolved
date: 2026-06-09
tags: [brain, llm, routing, latency, nvidia, gemini, groq, rate-limit, voice]
related: [[provider-circuit-breaker]] [[groq-daily-cap-routing-storm]] [[groq-failfast-client]] [[free-llm-tier-audit-2026-06]]
---

# All-rung cascade outage: hung NVIDIA × 15s timeout = ~15.5s every voice turn

## Symptom

Operator (2026-06-09 ~23:20 UTC, live VC): *"right now it is extremely slow …
ITS INSANELY SLOW."* Every addressed turn took **~15.3–16.3s to first word**
(`llm_ms=15399 / 15713 / 15724 / 15801 / 15296 / 16308`).

## Root cause — four simultaneous failures, one dominant

Live probes from inside the container confirmed each rung's state:

1. **Groq primary hard-capped** (daily TPD): `Limit 200000, Used 199,958` —
   correctly skipped by the breaker. ✓ working as designed.
2. **Gemini rung burst-throttled**: 429 body = `generate_content_free_tier_requests,
   limit: 20, model: gemini-2.5-flash-lite … Please retry in 46.4s` — the free
   tier is **~20 requests/min per model**, and with Groq capped, every addressed
   turn routes through Gemini; a busy VC bursts past 20/min. Two parser gaps
   meant the breaker used its 60s default instead of the server's 46s: Gemini
   says "**retry** in" (regex only matched "try again in") and puts it in the
   **response body** (`raise_for_status` doesn't include the body in `str(exc)`).
3. **Scout last-resort also 429ing** (44×/hour — its budget burned too).
4. **NVIDIA NIM provider-side outage — the actual slowness**: a bare 5-token
   "say hi" probe timed out at 20s. With rungs 1/2/4 skipped or 429ing, every
   turn fell to NVIDIA and waited the **full 15s httpx timeout** before failing
   over. The breaker deliberately did NOT cool down timeouts ("transient"), so
   the 15s was re-paid **every single turn** → the observed ~15.5s.

Additional root-cause discovery from the Groq 429 body: **`Requested 4006`** —
each routing call sends ~4k tokens (full system prompt), so Groq's 200k/day ≈
**only ~50 routed turns**. That is *why* the daily cap exhausts nightly. Tracked
as the next structural fix (routing-specific slim prompt), not changed tonight.

## Fix (all at the layer where each issue lives)

1. **Consecutive-timeout ejection** (`_note_model_timeout`): 2 timeouts in a row
   → model cooled for a short fixed 120s window → a hung provider leaves the hot
   path automatically and rejoins quickly after recovery. One timeout remains
   transient (no reaction). This **supersedes** the breaker's original
   "timeouts do NOT arm" rule — see the amended [[provider-circuit-breaker]].
2. **REST routing timeout right-sized 15s → 6s** (`_call_provider_with_tools`):
   routing calls normally complete <2s; the ceiling is hang-detection, not
   patience. Worst case before ejection is now 2×6s, not 2×15s.
3. **Breaker parses Gemini's actual signal**: regex extended to
   `(?:try again|retry) in`, and `_retry_after_seconds` now also searches
   `exc.response.text` (the 429 JSON body) — cooldowns now use Gemini's
   server-advised window.
4. **Second Gemini model rung** (`gemini_router_model_alt = "gemini-2.5-flash"`,
   on Google's confirmed free list): Gemini free-tier burst limits are
   **per-model**, so a second model is a separate ~20 RPM bucket — double burst
   capacity at identical latency. Not a "backup on a backup": it widens the
   exact bottleneck (per-model RPM) that throttled rung 2.

## Validation

- `tests/unit/test_provider_circuit_breaker.py` additions: Gemini "retry in"
  format, body-parsing, timeout classification, 2-consecutive arming (1 doesn't),
  success/429 reset the streak, alt-rung config guard.
- Post-deploy: during the next Groq-cap window expect `cooldown_set
  reason=consecutive_timeouts` if a provider hangs, no llm_ms ≥15s pinned at the
  old ceiling, and `provider=gemini` routes split across both models.

## Follow-ups

- **Routing prompt is ~4k tokens/call** → ~50 turns/day on Groq. A slim
  routing-only prompt is the structural fix for the nightly cap (high leverage,
  needs careful regression on routing quality — separate change).
- Re-benchmark `gemini-3.1-flash-lite-preview` **with thinking disabled**
  (Gemini 3.x defaults to reasoning; the earlier "42% spikes >2s" verdict in
  [[free-llm-tier-audit-2026-06]] likely measured thinking, not instability).
  Do it when quota is idle — not during a cap window.
