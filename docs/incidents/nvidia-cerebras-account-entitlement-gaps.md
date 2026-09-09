---
type: incident
status: active
date: 2026-09-08
tags: [llm, cascade, nvidia, cerebras, billing, account, model-freshness]
related: [[routing-tpm-ceiling-degrades-cascade]] [[disable-dead-vision-and-fallback-model-rungs]] [[provider-cooldown-registry]]
---

# NVIDIA and Cerebras rungs are down for account/billing reasons — not stale model IDs

## Symptom

[[routing-tpm-ceiling-degrades-cascade]] classified `nvidia_model` as a
"dead model" alongside four others (a stale/removed model *id*). Digging
into replacing it surfaced a materially different, larger problem for both
NVIDIA and Cerebras: **no model id on either provider currently works with
these credentials**, for reasons a config change cannot fix.

## Root cause — NVIDIA

`models.list()` on NVIDIA NIM returns the full public catalog regardless of
what a given key is entitled to invoke — unlike Groq/Gemini, where the
list is scoped to the key. This makes NVIDIA's listing endpoint useless as
a liveness check; only a real invocation proves anything. Trial invocation
of ~15 models found:

- Every model from the account's original entitlement tier (llama-3.1/3.2/3.3,
  mixtral-8x7b, nemotron-nano-8b, etc.) returns **410 Gone**, most citing
  the identical timestamp `reached its end of life on
  2026-08-26T09:00:00Z` — a platform-wide generation retirement, not
  independent per-model deprecations.
- Every model from NVIDIA's *current* generation tried (nemotron-nano-3-30b,
  gpt-oss-20b, phi-3.5-moe, mistral-7b-instruct, granite-3.0, yi-large)
  returns **403 Forbidden** — this key was never entitled to the
  post-migration lineup.

Net: this key has zero working chat-completions models on NVIDIA right
now. Requires re-provisioning at build.nvidia.com (new key or upgraded
entitlement) — not a code fix.

## Root cause — Cerebras

`CerebrasProvider._detect_best_model` (already a well-built auto-detection
system — lists live models, tests each with a real inference call, only
returns a model that actually works) was doing its job correctly and
reporting failure correctly. Live check:

- `GET /v1/models` → 200, returns exactly `gpt-oss-120b`, `qwen-3.8-27b`,
  `gemma-4-31b` (the catalog itself has moved on from every id in
  `_PREFERRED_MODELS`, though `gpt-oss-120b` happens to still match).
- `POST /v1/chat/completions` on **all three** live-listed models →
  **402 Payment Required**: `"Payment required to access this resource.
  Visit your billing tab."`

This is an account billing/quota state, not a model problem. No
replacement model id fixes a 402 on every model uniformly.

## Why this wasn't caught as billing sooner

The existing error handling swallows the specific status distinction:
`_test_model` logs `"Model test failed"` at DEBUG on any non-200 (404 or
402 look identical in the log), and the cascade's `_note_model_rate_limited`/
`_note_model_timeout` classifiers don't recognize 402/403/410 at all (fixed
in [[disable-dead-vision-and-fallback-model-rungs]] — `note_permanent_failure`).
So the log signature for "wrong model id" and "billing lapsed" were
identical: an ERROR-level "No Cerebras model works" at every startup,
giving no signal to distinguish them without live re-testing.

## A real code bug found investigating this

`CerebrasProvider.is_available()` returned `True` whenever the
detection-failure fallback model (`detected or model`) happened to land in
`_MIN_QUALITY_MODELS` — even though `_detect_best_model` had, moments
earlier, proven via real inference that model does not work (that's
*why* it fell back to the configured name at all). The cascade believed
Cerebras was healthy while every real call to it would 402. Fixed: a
`_detection_failed` flag set whenever `_detect_best_model` returns `None`,
checked first in `is_available()` regardless of quality tier.

## Fix

- Code: `CerebrasProvider.is_available()` bug fixed (above).
- Code: `ProviderCooldownRegistry` extended with permanent-failure
  classification so once these ARE re-entitled and then regress again,
  the cascade stops wasting a probe on every traversal instead of
  re-learning this the hard way a second time
  ([[disable-dead-vision-and-fallback-model-rungs]]).
- Account: **requires operator action**, not code:
  - NVIDIA: re-provision the API key at build.nvidia.com for the current
    model generation.
  - Cerebras: check the billing tab at cloud.cerebras.ai — either the free
    tier expired/was exhausted, or a payment method needs to be added.

## Validation

- Every claim above is a live API response captured in this session, not
  inferred from logs or assumed from documentation.
- `tests/unit/test_cerebras_provider.py::test_is_available_false_when_every_candidate_failed_inference`
  — mutation-verified regression guard for the `is_available()` bug.
- Full unit suite green (2015 passed).

## Addendum (2026-09-08) — NVIDIA re-provisioned, model chosen and eval'd

Operator generated a new NVIDIA API key at build.nvidia.com. Confirmed live
against the exact failure signatures above:

- `meta/llama-3.1-8b-instruct` (an old-generation model) still 410s —
  correctly, that generation really is EOL'd platform-wide, unrelated to
  entitlement.
- `nvidia/nemotron-3.5-lightning-30b-a3b` and `openai/gpt-oss-20b`
  (current-generation models that 403'd on the old key) now return real
  200 responses. **The entitlement gap is fixed for the new key.**

Chose `nemotron-3.5-lightning-30b-a3b` for `agent_nvidia_model` /
`PoobBrain.nvidia_model` after evaluating three live candidates against the
real `music_assistant` tool schema (not just a bare ping):

| Candidate | Correctness (4-5 routing prompts) | Reasoning-trace leak | Latency (median) |
|---|---|---|---|
| `nemotron-3.5-lightning-30b-a3b` | **all correct**, including NOT-music "flip a coin" → NO_TOOL | none, in either path | ~2.5s |
| `openai/gpt-oss-20b` (on NVIDIA) | hallucinated a nonexistent `flip_coin` tool call on the NOT-music case | none | ~3.5s |
| `nemotron-3-super-120b-a12b` | correct decision on NOT-music case | **leaked partial chain-of-thought** into the discarded no-tool content field | ~6s |

`gpt-oss-20b`'s failure is notable precisely because it's the *same model
weights* this project already trusts on Groq — the routing-accuracy defect
is specific to NVIDIA's hosting of it (different default sampling/params,
unconfirmed which), not the model itself. Not investigated further since a
working alternative was already found.

`nvidia_model` (the SEPARATE config field feeding browser-use, which needs
grammar-constrained structured output — a stricter requirement than plain
tool-calling) was deliberately **not** changed here — none of the three
candidates were tested against that requirement.

Cerebras remains fully open — no account action taken yet on that side.
This incident stays `active` until Cerebras is also resolved.

## Follow-ups

- Cerebras: still needs the billing-tab check at cloud.cerebras.ai.
  Re-verify live and flip to `resolved` once both providers are confirmed
  working.
- `nvidia_model` (browser-use) is still on the dead `qwen3-next-80b-a3b`
  default — now that the account itself is fixed, this just needs its own
  candidate eval against the grammar/structured-output requirement
  specifically, using the same re-provisioned key.
- The routing cascade tolerated both rungs being fully dead throughout
  (Groq + Gemini absorbed the load) — this was a capacity/redundancy loss,
  not an outage, worth stating plainly rather than overstating urgency.
