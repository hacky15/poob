---
type: decision
status: active
date: 2026-05-30
tags: [vlm, cascade, llm, cleanup, cost]
related: [[groq-gpt-oss-20b-swap]]
---

# Remove permanently-dead VLM rungs; harden permanent-error detection; demote the dead tiebreaker

## Context

A 12h audit (2026-05-30) found the VLM cascade healthy at the product level — the top-3 rungs (groq_vision, gemini_flash_lite, mistral_pixtral) reach voting consensus and carry 100% of delivered deals — but masking **four permanently-dead rungs** that re-fail on every restart and every sequential fallthrough:

| Rung | Failure | Cause |
|---|---|---|
| gemma (`gemma-3-27b-it`) | 404 NOT_FOUND, 0 successes ever | retired Google model id |
| together_vision (`Llama-Vision-Free`) | 402 Credit limit | no free tier without a card — violates $0 |
| openrouter_mistral (`mistral-small-3.1-24b:free`) | 404 No endpoints | model removed from OpenRouter |
| gemini_pro (`gemini-2.5-pro`, tiebreaker) | 429 on 4/4 disagreements | 100 RPD quota shared with the flash-lite workhorse, exhausts early |

Two compounding bugs:
- **`_is_permanent_error` missed the Google 404 shape.** It only caught `status_code==404` or the OpenRouter `"no endpoints"` string. langchain-google-genai raises `"404 NOT_FOUND"` / `"... is not found for API version ..."` with **no** `status_code` attribute, so a retired Google id fell through to the generic 600s consecutive-failure cooldown and **re-failed every ~10 min forever** instead of being parked for the session.
- **A 100%-429 tiebreaker** meant disagreements never actually tie-broke; the cascade already falls back to first-responder, so the tiebreaker was dead weight burning a Google-pool quota call.

There were also two **dead config fields** (`vlm_primary_provider`, `vlm_fallback_providers`) that `build_vlm_cascade` never reads (it hardcodes order/membership) — a no-op tuning trap.

## Decision

1. **Remove the three permanently-dead rungs** from `build_vlm_cascade` ([vlm_cascade.py](../../src/poob/llm/vlm_cascade.py)). groq_vision already covers the Meta-architecture voter; openrouter_nemotron covers the OpenRouter/NVIDIA fallback. No deal-output impact (the rungs never succeeded).
2. **Harden `_is_permanent_error`** to classify Google's `404`/`NOT_FOUND`/`"is not found for API version"` as permanent — so *any* retired Google model id is parked for the session, not looped on the 600s cooldown. This is the durable, scale-relevant fix (it generalizes beyond the one id).
3. **Demote `gemini_pro` from `is_tiebreaker`.** With no tiebreaker, no-consensus uses the first responder — already the documented, blessed behavior ([[groq-gpt-oss-20b-swap]] cascade philosophy; the operational-findings note states first-responder-on-disagreement is acceptable). gemini_pro stays a normal voter when it has quota. We deliberately did **not** add a working non-Google tiebreaker — that would re-introduce the tiebreak latency the cascade intentionally avoids, and would need its own superseding decision.
4. **Delete the dead config fields** `vlm_primary_provider` / `vlm_fallback_providers` (and the now-orphaned `gemma_vlm_model`, `gemma_vlm_rpd`, `together_api_key`, `openrouter_model`). `extra="ignore"` on AppConfig means stray env vars are harmless. No back-compat shim (per the no-dead-names rule — grep returns zero hits in code; retired names live only in the vault).

## Consequences

- The cascade has no permanently-404/402 rungs re-failing each restart — less wasted latency, cleaner logs, no false "provider_recovery" churn.
- A future retired Google model id will be parked, not looped — the class of bug is fixed, not just the instance.
- Re-adding a Gemma voter requires verifying a **currently-live** free multimodal Google id first (do not guess — the prior id 404'd); same for any replacement OpenRouter primary.

## Validation

- `tests/unit/test_vlm_voting.py::TestIsPermanentError`: Google `404 NOT_FOUND`, `"is not found for API version"`, OpenRouter no-endpoints, and Together 402 → permanent; 429 and 503 → NOT permanent.
- Full `test_vlm_voting.py` green; `import poob.llm.vlm_cascade` + `poob.config` clean; grep confirms zero code references to the removed config fields/rung names.
- Prod: dead-rung 404/402 lines should stop; no `gemma`/`together`/`openrouter_mistral` provider entries; no tiebreaker invocations.
