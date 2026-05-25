---
type: reference
status: active
date: 2026-03-29
tags: [vlm, llm, cascade, providers]
related: [[vlm-triage-pipeline]] [[google-vlm-ipm-undocumented-limit]] [[groq-gpt-oss-20b-swap]] [[drop-cerebras-from-cascade]]
---

# VLM cascade — per-provider operational behavior

## Summary

Empirical results from running the VLM cascade under real patrol load. The cascade order, transient-vs-permanent error handling, and the consensus model are all tuned from these numbers. Update when provider behavior changes.

## Provider performance (observed, March 29 2026)

| Provider | Avg Response | Reliability | Notes |
|----------|-------------|-------------|-------|
| Groq Vision (Llama 4 Scout 17B) | 0.5-1.5s | Excellent (14,400 RPD) | Best speed, adequate quality |
| Gemini Flash Lite | 1.5-3s | Good (1,000 RPD) | Consistent, high volume |
| Gemini Flash | 2-6s | Good but IPM-limited (250 RPD) | Hits RESOURCE_EXHAUSTED after ~8 evals |
| Mistral Pixtral 12B | 2-3s | Good (1,400 RPD) | Stable, predictable |
| Gemma 27B | 5-12s | Unreliable | ALWAYS cancelled in voting (too slow). Only useful as fallback. |

## Dead / permanent-error providers

- **Together.ai**: 402 "Credit limit exceeded" — no free tier without a payment method. Auto-disabled for 24h on first failure.
- **OpenRouter Mistral Small 3.1 24B**: 404 "No endpoints found" — model removed from OpenRouter. Auto-disabled for 24h.
- **OpenRouter Nemotron**: often timeout / cancelled. Low-priority fallback.
- **Ollama VLM**: requires local Ollama running. Emergency fallback only.

## Cascade order rationale

1. Gemini Flash — highest quality free VLM
2. Gemini Flash Lite — high RPD, fast, good quality
3. Groq Vision — very fast, highest RPD, adequate quality
4. Mistral Pixtral — stable, different architecture (diversity for voting)
5. Gemma 27B — slow but Google-diverse; only used when top 4 exhausted
6–10. Fallbacks (Together, OpenRouter, Gemini Pro tiebreaker, Ollama)

## Transient vs permanent error detection

The cascade distinguishes transient rate limits (429, RESOURCE_EXHAUSTED) from permanent errors (402 billing, 404 model not found, 401 auth). Permanent errors disable the provider for 24h (session lifetime). Eliminates wasted panel slots every eval.

## Voting consensus

`no_consensus` is frequent (~40% of evals). The first responder's opinion is used when three providers disagree. This means Groq Vision's opinion dominates when it fires first — acceptable because Groq's accuracy is adequate for deal detection and the alternative (blocking on a slow tiebreaker) would double evaluation time.

## Early-exit ≠ failure

When 2/3 voters agree and the third task is cancelled, the cancellation must NOT increment the consecutive-failure counter — otherwise healthy providers get demoted for 10 minutes because of successful early exits.

## Tool-caller (brain) cascade

Different from VLM but the same operational pattern. Original benchmark + cascade order from April 7 is preserved below for historical reference; **current production order has shifted** per [[groq-gpt-oss-20b-swap]] and [[drop-cerebras-from-cascade]].

### Current production order (as of 2026-05-24)

`_groq_with_tools` in [brain/poob.py](../../src/poob/brain/poob.py):

1. Groq `openai/gpt-oss-20b` (primary) — Harmony tool-call format, NOT affected by Groq's parser regression on Meta's `<function=...>` wrapper that broke llama-3.3-70b
2. NVIDIA NIM `qwen3-next-80b` — different provider, sidesteps Groq rate limits
3. Groq `meta-llama/llama-4-scout-17b-16e-instruct` — last resort only; over-routes to music_assistant, kept for emergency fallback

Removed from the cascade:
- Groq `llama-3.3-70b-versatile` — chronic 400 `tool_use_failed` from Groq's parser misreading the model's native `<function=...>` syntax. Open since Jul 2025 upstream.
- Cerebras `qwen-3-235b-a22b-instruct-2507` — chronic 429-rate-limited, added 200-300ms of dead retry tax per call without ever serving a successful one.

### Historical benchmark (April 7 2026, pre-shift)

Preserved so future audits can trace WHY the order changed. The latency numbers were valid at the time but the practical reliability ratings drove the order shift more than raw latency:

| Provider / Model | Play Latency | Skip Correct? | Tool Support |
|---|---|---|---|
| Groq llama-3.3-70b-versatile | 925ms | Yes | Full **(now broken via Groq parser regression)** |
| Groq llama-4-scout-17b | 858ms | Yes | Full but over-aggressive ("Did you get offended?" → plays "Big Ole Freak") |
| Cerebras qwen-3-235b | 752ms | Yes | Full **(429-throttled in production; dropped)** |
| NVIDIA qwen3-next-80b | 930ms | Yes | Full |
| Groq llama-3.1-8b-instant | 518ms | ERR 400 | Partial (fails on short msgs) |
| Groq llama-3.3-70b-specdec | - | ERR 400 | Broken |

## Usage in this project

- [vlm/cascade.py](../../src/poob/vlm/cascade.py) — cascade orchestration
- [vlm/providers/](../../src/poob/vlm/providers/) — per-provider adapters
- [brain/poob.py](../../src/poob/brain/poob.py) — tool-caller cascade (`_groq_with_tools`)
