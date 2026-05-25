---
type: incident
status: resolved
date: 2026-04-23
tags: [brain, voice, latency, groq, cerebras]
related: [[poobbrain-architecture]] [[brain-routing-audit]] [[groq-gpt-oss-20b-swap]] [[drop-cerebras-from-cascade]] [[text-casual-fallback-bypass-deal-agent]]
---

# Casual voice chat reply taking 13+ seconds end-to-end

## Resolution (2026-05-24 audit)

The three short-term proposals named in §"Fix (proposed, not yet implemented)" below have all shipped:

- Groq `gpt-oss-20b` pinned as primary tool-routing model → [[groq-gpt-oss-20b-swap]] (2026-04-23)
- Cerebras 235B leg dropped from the cascade entirely → [[drop-cerebras-from-cascade]] (2026-04-23)
- Casual-chat path bypasses the deal sub-agent when router returns empty → [[text-casual-fallback-bypass-deal-agent]] (2026-05-06)

The medium-term proposals (per-cascade-segment structured timing, distinct latency budgets per path) did not ship as named, but the underlying 13s symptom was driven by the routing-LLM cascade chain, which the three shipped fixes addressed. No further reproduction has been logged.


## Symptom

User (Ben) said "Hey, Poob. Are you even there?" at `2026-04-24T01:29:54.871Z`.
Response posted at `01:30:08.436Z` — **13.565 seconds end-to-end**.

Log line:
```
Response complete
  response="*yawn* Yeah, I'm here, Ben. Deployments got me thinkin' about long-distance relationships..."
  total_ms=13563
  user='Ben (hacky15)'
```

For comparison, the immediately-following tool-call request ("Hey, Poob. Play jah jah jah bass jackers remix") completed in 5.1s end-to-end. The casual-chat path is **~3x slower than the tool-call path**, which is the inverse of what should happen — tool calls add LLM iterations, casual chat should be a single-shot.

## Root cause (likely)

Running shortly before the 13.5s response, the log shows:

```
vlm.voting_partial_failures
  failures=["gemini_flash: Error calling model 'gemini-3-flash-preview' (RESOURCE_EXHAUSTED): 429"]
  succeeded=['groq_vision', 'gemini_flash_lite']

vlm.tiebreaker_failed
  cooldown_s=3600.0
  error="Error calling model 'gemini-2.5-pro' (RESOURCE_EXHAUSTED): 429"

Tavily search failed
  response_body='{"detail":{"error":"This request exceeds your plan\'s set usage limit..."}}'
  status=432
```

The environment is actively rate-limited across multiple providers. The cloud LLM cascade is `cerebras:qwen-3-235b → groq:llama-3.3-70b → ollama:local`. If Cerebras is rate-limited (same pattern as the VLMs above, or shared quota with another caller), the cascade falls to Groq 70B, which has a 30 RPM / 14,400 RPD free-tier limit that's easy to hit during active user interaction plus simultaneous patrol cycles.

Each 429 retry costs 1-2s. A cascade of Cerebras 429 → Groq 429 → Ollama (local, slow) would plausibly add 5-10 seconds on top of normal inference time, reaching the observed 13.5s.

Separate contributing factor: [[poobbrain-architecture|PoobBrain]] uses a 235B Cerebras model (`qwen-3-235b-a22b-instruct-2507`) as primary for tool-routing classification. Cold-start on Cerebras for a 235B model is reported to be several seconds for first-token-out even without rate limits.

## Fix (proposed, not yet implemented)

Short term:
- Pin **Groq `gpt-oss-20b`** (the commit `97bc1ce fix(brain): swap Groq primary to gpt-oss-20b` already put this in the chain) as the PRIMARY for casual text chat routing. Keep Cerebras 235B as reasoning/agent-backend.
- Set max-retry delay per provider to a low ceiling (e.g., 2s) so the cascade can skip a rate-limited provider quickly instead of waiting for its per-provider retry budget to exhaust.

Medium term:
- Emit structured timing logs per cascade segment so the "13.5s" breakdown isn't inferred — we should see exactly where the time goes: routing-LLM, agent-LLM (if triggered), personality-wrap LLM, TTS synthesis, audio playback start.
- Distinguish casual-chat latency budget from tool-call budget. Casual chat should have a strict 2-3s total budget; tool calls 5-10s.

## Validation

After a fix, watch for:
- `Response complete total_ms=N` on casual chat messages should be < 3000ms p50, < 5000ms p95.
- Per-provider timing histograms (if/when added) should show hot-path on one fast provider, not a cascade of timeouts.

## Adjacent evidence (not this issue but relevant)

- Tavily free-tier is exhausted (status 432, visible in same log window). Search-provider cascade needs to drop Tavily from retail-lookup rotation — currently it's wasting ~1-2s per query on the 432 before falling through to Serper/SearXNG.
- SerpAPI also disabled for session (`SerpAPI disabled for session (rate limited)`). Google Cloud Vision quota is healthy but Google Flash is hitting `RESOURCE_EXHAUSTED` — that's the 250 RPD free tier.

## Out of scope

This incident is diagnosed but **not fixed** in the scanner-chat session. The fix belongs in [[poobbrain-architecture|brain/poob.py]] and possibly [[vlm-cascade-operational-findings|the shared cascade utilities]]. Assigned to the brain/voice chat.
