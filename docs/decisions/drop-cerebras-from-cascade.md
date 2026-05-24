---
type: decision
status: active
date: 2026-04-23
tags: [brain, llm, cascade, cerebras, latency]
related: [[brain-routing-audit]] [[groq-gpt-oss-20b-swap]] [[poobbrain-architecture]] [[vlm-cascade-operational-findings]]
---

# Drop Cerebras qwen-3-235b from the tool-call cascade

## Context

Cerebras `qwen-3-235b-a22b-instruct-2507` used to sit in slot #2 of the tool-call cascade as a different-provider fallback for Groq rate limits. Production logs have been showing chronic 429s from Cerebras:

```
Tool detection failed, trying next
  error=Client error '429 Too Many Requests' for url 'https://api.ce...'
  model=qwen-3-235b-a22b-instruct-2507  provider=cerebras
```

It hasn't served a successful tool call in any of the recent sessions — it's pure latency tax (~200-300ms per request paid every time the cascade falls through Groq).

The Groq primary is now `openai/gpt-oss-20b` which is not affected by the `llama-3.3-70b-versatile` parser regression (see [[groq-gpt-oss-20b-swap]]), so falling through Groq is much rarer. When it does happen, skipping straight to NVIDIA NIM is strictly better than a 429 round-trip through Cerebras first.

## Decision

Remove Cerebras from `_groq_with_tools`'s `providers` list. New cascade:

1. Groq `openai/gpt-oss-20b` (primary)
2. NVIDIA NIM `qwen3-next-80b-a3b-instruct` (fallback)
3. Groq `meta-llama/llama-4-scout-17b-16e-instruct` (last resort; known to misroute to music so kept purely as don't-die-on-everything)

The `self.cerebras_api_key` field stays on `PoobBrain` for now — other callers (brain cascade, `_fallback_generate`) may still reference it. Cerebras isn't ripped out of the project; just dropped from this specific path.

## Alternatives considered

- **Demote Cerebras below NVIDIA instead of removing.** Doesn't help — if NVIDIA succeeds, Cerebras doesn't run. If NVIDIA fails, we need the last-resort (Scout). Cerebras in third slot never runs.
- **Wait for Cerebras to lift the rate limit.** Based on the logs, it's been rate-limited for days. Waiting is just paying the tax indefinitely. We can bring it back when benchmarks show a win.
- **Keep Cerebras gated on a feature flag.** Over-engineered for a one-line removal.

## Exit criterion

Active after 24h of normal production use:

- `Tool detection failed ... provider=cerebras` events disappear from prod logs.
- No regression in tool-routing correctness — routes that used to fall through to Cerebras successfully (hypothetically) now succeed on Groq primary, or fall through to NVIDIA without user-visible difference.
- Median `poob.tool_route` latency drops by ≥150ms on requests that previously reached Cerebras.

## Rollback

Restore the two-line `if self.cerebras_api_key: providers.append(("cerebras", "qwen-3-235b-a22b-instruct-2507"))` block. Zero behavior change beyond that.

## Results

<!-- Filled in after 24h of prod use. -->
