---
type: decision
status: active
date: 2026-04-23
tags: [brain, llm, groq, tool-calling, latency]
related: [[brain-routing-audit]] [[poobbrain-architecture]] [[voice-pipeline-optimization-prompt]]
---

# Swap Groq primary to `openai/gpt-oss-20b` + recover tool_use_failed from `<function=...>`

## Context

Groq's `llama-3.3-70b-versatile` has been returning HTTP 400 `tool_use_failed` on every tool-call attempt in production. April 2026 voice-pipeline research confirmed this is a Groq-side parser regression — the model emits Meta's native `<function=name>{...}</function>` wrapper and Groq's OpenAI-compat parser cannot convert it. Known unresolved since July 2025. Wastes ~500ms per request as the cascade falls through to Cerebras (which is rate-limited) and eventually NVIDIA.

## Decision

Two independent changes in one commit:

**A. Swap primary model.** `groq_model` default changes from `llama-3.3-70b-versatile` to `openai/gpt-oss-20b`. The OpenAI-Harmony-format model is not affected by the parser regression and is reported at 1000+ t/s on Groq. `llama-3.3-70b-versatile` is removed from the primary slot; the rest of the cascade (Cerebras qwen-3-235b, NVIDIA qwen3-next-80b, Groq Scout 17B as last resort) is unchanged.

**B. Client-side recovery regex.** On any Groq `BadRequestError` with `error.code == "tool_use_failed"`, `_recover_tool_call_from_groq_400` parses `failed_generation` for `<function=NAME>{...JSON...}` and reconstructs the tool call. Uses `json.JSONDecoder.raw_decode` to correctly handle nested braces. Logs `groq.tool_call_recovered_from_function_tag` on success so we can measure recovery rate.

Recovery is defensive — it benefits any future Groq model that exhibits the same regression, not just `llama-3.3-70b-versatile`.

## Alternatives considered

- **Only client-side recovery, keep `llama-3.3-70b-versatile`** — leaves us paying a 500ms round-trip to get a 400 then recover. Model swap is strictly better if `gpt-oss-20b` routes at least as well.
- **Self-hosted Qwen3-4B-Instruct-2507 on 4090** (report's longer-term recommendation) — bigger effort, deferred until the cheap swap is validated.
- **Drop Cerebras from the cascade entirely** (429 chronic) — reasonable but separate concern; not bundled here.

## Baseline (to fill in before committing)

- Groq `Tool detection failed` count per hour: **TBD** (grep production logs).
- Median time from `Dual: wake word addressed` to `poob.tool_route`: **TBD**.
- Tool-call correctness on a handful of real addressed transcripts: **TBD** (user check during normal voice session).

## Exit criterion

Status flips to `active` if, after 24 hours of normal usage on the new default:

- **Primary metric**: `Tool detection failed` events against Groq drop to ~0, OR when they do happen, a matching `groq.tool_call_recovered_from_function_tag` log follows within the same request.
- **Guard metric**: tool-call correctness (does Poob pick the right tool with the right args on addressed utterances?) stays ≥ current. Measured subjectively by the user during normal voice session.
- **Guard metric**: tool-hallucination rate (from [[music-tool-hallucination]]) stays flat — `music.play hallucinated from context — drop` counter does not increase.

If `gpt-oss-20b` routes worse than llama-3.3-70b, swap back via `GROQ_MODEL=llama-3.3-70b-versatile` in the Komodo env — the recovery regex still earns its keep on the failing model.

## Rollback

- **Model swap**: one env var (`GROQ_MODEL`) on homelab Komodo stack.
- **Recovery regex**: revert the commit, or set a feature flag later if we want to toggle it independently.

## Results

<!-- Filled in after 24h of production. Status flips to active / superseded based on exit criterion. -->
