---
type: decision
status: active
date: 2026-06-22
tags: [brain, llm, routing, tokens, rate-limit, latency, cascade]
related: [[slim-routing-prompt]] [[slim-tool-schemas]] [[gemini-router-prefer-2.5-ga-over-3.1-preview]] [[groq-daily-token-cap-degrades-routing]]
---

# Give the tool-router a trimmed message copy (last few turns, no crosstalk)

## Context

A 2026-06-22 audit of "crazy delay in the last hour" found 8–12s wake→audio
times. Root cause: **both daily free-tier caps were exhausted** — Groq
`gpt-oss-20b` TPD (200k tok/day) hit (cooldowns of 969s/1111s) and Gemini's
project RPD hit (gemini-2.5-flash-lite returning `429`, 53–58s cooldowns) — so
routing fell to the slow **NVIDIA** rung (plus a 6s `gemini-3.1` timeout tax,
fixed separately in [[gemini-router-prefer-2.5-ga-over-3.1-preview]]).

Why the daily caps blow so fast: **the routing call dragged the full 15-turn
history AND the passive crosstalk transcript into every request.** [[slim-routing-prompt]]
and [[slim-tool-schemas]] already trimmed the *prompt* and *schemas*, but the
*conversation context* was still ~full. In a 5–9 person VC the crosstalk block
alone is large, so each routing call cost ~3.3–5k tokens — ~50 routes and the
Groq daily cap is spent.

Routing is **intent classification**: it needs the current command + a couple
recent turns for follow-ups ("more", "yes", "that one"); current-track and
deal-session context already live in the routing *system prompt*. It does NOT
need 15 turns or the crosstalk (the crosstalk is for the persona *reply*, so
Poob can reference what others said — a generation concern).

## Decision

The routing call gets a **trimmed copy** via `PoobBrain._trim_for_routing`:
system prompt + the last `_ROUTING_HISTORY_TURNS` (=4) conversational turns, with
the `[Recent conversation you've been listening to: …]` crosstalk wrapper
stripped from the final user turn. The original `messages` is untouched and
still drives the casual reply with full history + crosstalk (generation is
unchanged — no reply-quality regression).

Measured ~44% fewer routing chars on a representative busy-VC call (more with
heavier crosstalk), roughly **doubling routes-per-day before the caps bite** →
fewer fall-throughs to the slow NVIDIA rung.

Shipped alongside: the flaky `gemini-3.1-preview` overflow rung is disabled
(`gemini_router_model_alt=""`, guarded append) — see
[[gemini-router-prefer-2.5-ga-over-3.1-preview]].

## Alternatives considered

- **Trim the shared `messages` in `_build_messages`.** Rejected — generation
  reuses it (`_rebuild_messages_no_tools`), so trimming there would strip the
  casual reply's context too. A routing-only copy keeps generation intact.
- **Lower `max_history`.** Rejected — that degrades generation coherence for
  everyone to save routing tokens; the split is surgical.
- **Pay for a higher Groq/Gemini tier.** Out of scope ($0 constraint). This
  extends the free caps instead.

## Consequences

- Routing accuracy preserved for the common cases (follow-ups covered by the
  last 4 turns; track/deal context in the system prompt). The rare
  crosstalk-dependent command ("play the song Owen just mentioned") loses that
  signal at the routing layer — acceptable for the large token win.
- The daily-cap ceiling itself is unchanged (a free-tier reality,
  [[groq-daily-token-cap-degrades-routing]]); this makes the budget last ~2x
  longer and the fallback path (2.5 → NVIDIA) shorter.

## Validation

`tests/unit/test_slim_routing_prompt.py::test_trim_for_routing_keeps_last_turns_and_strips_crosstalk`
(system kept, last N turns only, crosstalk stripped, original untouched).
`test_provider_circuit_breaker` updated for the disabled alt rung.
