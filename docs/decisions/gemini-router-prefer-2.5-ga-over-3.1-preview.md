---
type: decision
status: active
date: 2026-06-22
tags: [brain, llm, cascade, gemini, tool-calling, latency, router]
supersedes: [[gemini-tool-router-rung]]
related: [[gemini-tool-router-rung]] [[groq-daily-token-cap-degrades-routing]] [[groq-gpt-oss-20b-swap]] [[cascade-outage-nvidia-hang-gemini-rpm]]
---

# Prefer GA gemini-2.5-flash-lite over the 3.1-flash-lite PREVIEW as the primary Gemini router rung

## Context

[[gemini-tool-router-rung]] added Gemini as the tool-router failover for when Groq's
`gpt-oss-20b` daily token cap is spent ([[groq-daily-token-cap-degrades-routing]]), and an
unloaded single-call benchmark made **`gemini-3.1-flash-lite-preview`** the primary Gemini rung
(~587 ms, "smarter") with `gemini-2.5-flash-lite` as the overflow alt.

A multi-agent audit of the **2026-06-22 busy-VC sessions** (5–9 users, ~38 addressed routes)
contradicted that benchmark under real load:

- **`gemini-3.1-flash-lite-preview` timed out (>6 s httpx ceiling) on 9 of ~23 calls (~39%)** —
  each timeout burns the full 6 s before falling through, and two streaks armed the 120 s
  consecutive-timeout cooldown.
- **`gemini-2.5-flash-lite` routed 14/14 with zero timeouts** in the same window (same network,
  same minutes — controls for a transient Google blip).
- Because Groq's TPD was exhausted mid-session, the Gemini rung carried most routing, so the
  3.1 flakiness was the **dominant end-to-end latency** (worst case wake→audio **10.6 s**, almost
  entirely 3.1 hanging before 2.5 answered in <1 s).

The earlier ~587 ms 3.1 number was unloaded/single-call; it does not reflect the preview model's
tail latency under sustained multi-user routing.

## Decision

Swap the Gemini rung order: **`gemini-2.5-flash-lite` (GA) is the primary Gemini router rung;
`gemini-3.1-flash-lite-preview` is demoted to the overflow alt.**

- `config.agent_google_model` → `gemini-2.5-flash-lite` (wired to the brain's primary gemini rung at main.py:575).
- `PoobBrain.gemini_router_model_alt` default → `gemini-3.1-flash-lite-preview`.

Rationale: a **router** needs to be fast and reliable, not "smart" — routing is a small
classification (action enum + a couple args), and 2.5-flash-lite handled it 14/14. Reliability
under load beats a marginal unloaded-benchmark speed/quality edge. 3.1 stays in the cascade as the
rarely-hit second Gemini bucket (it still succeeds ~61% of the time), so no capacity is lost.

Also shipped alongside (observability, not behavior): the cascade's "Tool detection failed"
warning now logs `type(exc).__name__` when the exception message is empty — httpx timeouts
stringify to `''` and were invisible in triage all night (the 3.1 hangs read as blank `error=`).

## Alternatives considered

- **Bump the 6 s gemini timeout to ~8 s.** Rejected as the first lever: the 6 s `httpx.AsyncClient`
  is SHARED with NVIDIA + Cerebras and was deliberately right-sized 15 s→6 s after the
  [[cascade-outage-nvidia-hang-gemini-rpm]] incident. Bumping it flat re-widens that blast radius;
  it would require a per-provider timeout split. Demoting the flaky model is lower-risk and
  data-backed.
- **Drop 3.1 entirely.** Rejected — it's a useful second RPM bucket at ~61% success for the rare
  overflow case; no reason to remove capacity.
- **Promote a Gemini rung above Groq for VC** (Groq is the TPD bottleneck). Deferred — bigger
  cascade reorder; the prior "Groq ~550 ms vs Gemini ~710 ms latency wash" benchmark
  ([[gemini-tool-router-rung]]) needs re-deriving under load before that's justified.

## Consequences

- When Groq is rate-limited/TPD-exhausted (common on a busy night), routing now hits the reliable
  2.5 rung first — worst-case routing drops from ~6–10 s (3.1 hang → fallback) to ~1–2 s.
- Reverses [[gemini-tool-router-rung]]'s 3.1-primary choice; that note is otherwise still valid
  (Gemini-as-Groq-failover remains correct). Marked superseded.
- Groq TPM/TPD exhaustion itself is unchanged — that's the free-tier capacity ceiling
  ([[groq-daily-token-cap-degrades-routing]]), a $0 constraint, not a code bug. This change makes
  the *fallback* fast, it doesn't add Groq capacity.

## Validation

`tests/unit/test_provider_circuit_breaker.py` updated to assert the new order (2.5 primary,
3.1 alt). `tests/unit/test_brain_gemini_router.py` already exercises the rung with 2.5. Will
verify in prod that gemini routing `llm_ms` drops and the empty-`error=` timeouts become
greppable by exception type.
