---
type: decision
status: active
date: 2026-09-08
tags: [llm, cascade, model-freshness, groq, gemini, resilience]
related: [[routing-tpm-ceiling-degrades-cascade]] [[provider-cooldown-registry]] [[nvidia-cerebras-account-entitlement-gaps]] [[voice-llm-model-deprecated-and-never-wired]]
---

# Six more dead/stale model rungs — replaced with live-verified ones, or cleanly disabled where none exist

## Decision

Following [[routing-tpm-ceiling-degrades-cascade]]'s finding of five dead
model ids, each was re-verified live and either replaced or disabled:

| Config field | Was | Live status (2026-09-08) | Fix |
|---|---|---|---|
| `groq_vision_model` | `meta-llama/llama-4-scout-17b-16e-instruct` | Groq's **entire vision-model category is gone** — zero vision-capable models in the catalog, not just this one | Disabled (`""`); both wiring sites gated |
| Groq Scout last-resort routing rung (hardcoded in `poob.py`, not a config field) | `meta-llama/llama-4-scout-17b-16e-instruct` | absent | `qwen/qwen3.8-27b`, smoke-tested for real tool-call emission |
| `agent_google_model_fast` | `gemini-3-flash` | no such id | `gemini-3.6-flash` (~1.7s, consistent) |
| `browser_google_model` | `gemini-2.0-flash` | absent | `gemini-3.5-flash-lite` (works; 0.6–8s spread tolerated for browser-automation's already-slow wall clock) |
| `nvidia_model` / `agent_nvidia_model` | `qwen/qwen3-next-80b-a3b-instruct` | **platform-wide EOL**, see below | Not replaced — see [[nvidia-cerebras-account-entitlement-gaps]] |
| Cerebras (all models) | `qwen-3-235b-a22b-instruct-2507` etc | **402 Payment Required**, account issue | Not a model problem — see [[nvidia-cerebras-account-entitlement-gaps]] |

## Why some got a straight swap and one didn't

The operator's standing rule is: no brain-model change ships without
evidence it's at least as good as what it replaces. That rule presupposes
the current model *works*. Every rung here was independently confirmed
100% non-functional (every real call errors) before any replacement was
picked — going from a guaranteed-0% baseline to any working model is not a
quality judgment call, it's restoring basic function. Each replacement was
still smoke-tested for real capability before shipping (not just presence
in a `models.list()` response — see [[nvidia-cerebras-account-entitlement-gaps]]
for why that check alone is insufficient for NVIDIA specifically):

- `qwen/qwen3.8-27b` (Scout replacement): tested actual tool-call emission
  against the real `music_assistant` schema shape — returned a correct
  `{"action":"play","query":"nightcore"}` call, not just a 200 response.
- `gemini-3.6-flash` / `gemini-3.5-flash-lite`: tested plain generation
  live, with repeated timing samples (the `browser_google_model` candidate
  swung 0.6s–8s across 3 identical calls — a genuine finding, not noise;
  it was still chosen for that role specifically because browser
  automation's own page-load latency already dwarfs LLM call variance,
  whereas `agent_google_model_fast`'s explicit "fast" requirement steered
  toward the more consistent `gemini-3.6-flash` instead).

The one deliberately deferred: `gemini_router_model` (currently
`gemini-2.5-flash-lite`, live and functional, just several generations
behind — `3.5`–`3.8-flash` are all live on this key). This rung directly
drives tool-routing accuracy, and [[routing-tpm-ceiling-degrades-cascade]]
already found the *current* weak-Gemini-rung behavior producing real
misroutes. Swapping it needs the full accuracy-oracle eval
(`tests/manual/bench_router_providers.py`), not a smoke test — deferred to
its own pass rather than folded into this "restore dead things" sweep.

## Also fixed: a real logic bug found along the way

`CerebrasProvider.is_available()` returned `True` whenever the
detection-failure fallback model happened to be in the quality-tier set —
even though `_detect_best_model` had *just* proven, via a real inference
call, that model doesn't work. See [[nvidia-cerebras-account-entitlement-gaps]]
for the fix.

## Also fixed: permanent failures never cooled down

The NVIDIA rung's 410-Gone errors matched neither the routing cascade's
rate-limit classifier (429-shaped) nor its timeout classifier — so it was
re-probed on every worst-case cascade traversal, forever, with zero chance
of ever succeeding. `ProviderCooldownRegistry` (docs/decisions/
provider-cooldown-registry.md) gained `is_permanent_failure_error`
(402/403/404/410) and `note_permanent_failure` (long fixed cooldown, no
server-advised window exists for this class), wired into the same
except-block as the existing rate-limit/timeout calls.

## Validation

- `tests/unit/test_vlm_cascade_build.py` (2, new): the `groq_vision` rung
  is present/absent correctly based on `groq_vision_model`. Mutation-
  verified: removed the guard, confirmed the "skipped when empty" test
  failed, restored, confirmed both pass.
- `tests/unit/test_cerebras_provider.py` (+1): `is_available()` is False
  when detection fails entirely, regardless of the fallback model's
  quality tier. Mutation-verified.
- `tests/unit/test_provider_cooldown_registry.py` (+6),
  `tests/unit/test_provider_circuit_breaker.py` (+2): permanent-failure
  classification and cooldown, at both the registry and `PoobBrain`
  delegation levels.
- Full unit suite: 2015 passed, 1 skipped, no regressions.
- Every "dead" claim verified against the live provider catalog/API
  directly, same session, not carried over from an earlier check.
