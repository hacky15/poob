---
type: decision
status: active
date: 2026-06-15
tags: [brain, llm, routing, tool-calling, latency, rate-limit]
related: [[slim-routing-prompt]] [[groq-daily-token-cap-degrades-routing]] [[gemini-tool-router-rung]] [[music-player-architecture]] [[brain-routing-audit]]
---

# Compress the routing tool schemas 36% (MUSIC_TOOL 1846 → 1165 tokens)

## Context

The 2026-06-15 forensic audit corrected a long-standing assumption: last night's
Groq routing 429 was **not** the per-DAY token cap (200k TPD) everyone modelled,
it was the per-**MINUTE** ceiling — `tokens per minute (TPM): Limit 8000, Used
5164, Requested 3617`. Bursty back-to-back voice commands blow 8k TPM because
**each routing call ships ~2.5–3k tokens**, dominated by the tool schemas sent
on every call. `MUSIC_TOOL` alone was **1846 tokens**; `DEAL_TOOL` 205.

Operator decision (prod data showed Groq ~550–760ms vs Gemini ~710–750ms — a
latency wash, so flipping the front slot buys availability, not speed): **attack
the token cost at the source** rather than swap providers. Fewer tokens/route →
more TPM headroom for every provider, no deterministic gate added, the proven
Groq-primary cascade unchanged.

## Decision

Compress `MUSIC_TOOL` by removing **redundancy and verbosity, not guidance**:

- **`action` description (468 → ~190 tok):** dropped the prose that re-explained
  self-evident enum names (skip/pause/resume/stop/shuffle/loop/now_playing/
  queue/clear are "self-explanatory"); kept only the genuine disambiguations
  (play vs queue_many, previous vs replay vs restore, volume vs volume_up/down,
  apply_effect vs list_effects, seek, move/remove positions, playlists, leave).
- **`effect` (324 → ~150):** kept the canonical preset list + the load-bearing
  nuance ('add reverb' usually means `slowed_reverb`, distinct from reverb-only)
  + the non-obvious aliases (max bass→ultrabass, earrape→overload, vader
  voice→darth_vader); dropped the per-preset parentheticals.
- **`time` (141 → ~55):** the handler (`parse_seek_input`) parses all formats,
  so the schema just says "pass the time phrase verbatim; the bot parses it."
- **fn description, `value`, `mode`, `query`, `tracks`:** compressed prose,
  preserving the **query anti-context-borrow rule** (never borrow a song title
  from a prior turn — root of several mis-routes), the volume extreme map, and
  the flip-a-coin non-music guard.

Net: **1846 → 1165 tokens (−36%)**. No actions (30) or params (12) dropped. The
detailed value-parsing already lived in the handlers (`parse_seek_input`,
`resolve_effect_chain`) — the schema was carrying handler-level prose the
routing LLM never needed.

## Alternatives considered

- **Flip to Gemini-primary** ([[gemini-tool-router-rung]] is already the
  fallback): rejected as the *first* lever — latency is a wash and it needs its
  own model slot to dodge VLM-voter RPM contention. Still on the table as a
  second step if TPM pressure persists after the trim.
- **Control-verb short-circuit** (skip the LLM for skip/stop/volume): rejected —
  it's the deterministic-gate expansion the operator already declined
  ([[groq-daily-token-cap-degrades-routing]] "don't make `_music_safety_net` do
  more") and conflicts with the agentic-mastery north-star.
- **Move effect aliases / volume extremes into the handler** (deterministic
  normalization in code, even slimmer schema): deferred — the 'reverb'
  ambiguity (bare "reverb" = slowed_reverb by convention, vs the reverb-only
  preset) makes a flat handler alias map lossy. Worth a follow-up with a proper
  alias resolver, but compression alone already won 36% at zero accuracy risk.

## Consequences

- Each route is ~680 tokens lighter → ~5 routes/min fit under 8k TPM where ~3
  did before. Meaningful burst headroom at $0.
- The slim schema is locked by `tests/unit/test_tool_schema_budget.py` (budget
  ceiling + every action/param + the load-bearing content checks) so it can't
  silently re-bloat.

## Validation

- `tests/unit/test_tool_schema_budget.py` — 1350-token ceiling, 30 actions + 12
  params preserved, load-bearing guidance (anti-borrow, reverb nuance, seek
  delegation, volume extremes, non-music guard) present.
- Live routing benchmark (`tests/manual/bench_router_providers.py`) on the
  trimmed schema vs the MATRIX oracle — accuracy held (see commit).
- Full unit suite green.
