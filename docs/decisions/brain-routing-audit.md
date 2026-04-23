---
type: decision
status: active
date: 2026-04-22
tags: [brain, llm-routing, tool-calling]
related: [[poobbrain-architecture]]
---

# Brain routing — LLM cascade, no keyword fallback

## Context

Earlier brain code had a `_TOOL_INTENTS` keyword map that bypassed the LLM when Groq failed, routing based on surface tokens ("scan", "wishlist", etc.). This was a bandaid over Groq unreliability and was fragile — any novel phrasing missed the routing.

## Decision

**Removed `_TOOL_INTENTS` entirely.** No keyword-based fallback. When Groq fails, the brain routes through `_handle_deal`, which uses the deal agent's own LLM cascade (Groq → NVIDIA → Gemini → Ollama). The deal agent has native tool calling across every provider.

Casual messages pass through the agent quickly with no tool calls and get personality-wrapped. The LLM is always the router; if one provider fails, the cascade picks up with another provider that also supports tool calling.

## Secondary heuristic (kept, not a bandaid)

`_groq_with_tools` has a small `tool_signals` tuple used to decide whether to continue the cascade when the current provider returned text instead of a tool call. Mechanism: if the last user turn contains strings like `"wishlist"`, `"watchlist"`, `"scan"`, `"my list"` AND the first provider returned text-only, the next provider gets a chance to tool-call.

This is provider-cascade stability, not intent routing. It does NOT bypass the LLM — it just keeps the cascade going through providers when the first one was text-only despite a signal. If none tool-call, the text response stands.

## Known weakness

The `tool_signals` list is English-only and hardcoded — novel phrasings ("hey check on my wishes") won't trigger cascade retry. In practice the first Groq model usually routes correctly; this is a safety net for weeks when Groq 70B is degraded. Acceptable to leave; expand only if tool-miss telemetry shows a miss rate worth the cost.

## Consequences

- Casual chat is unaffected: single Groq call, no cascade.
- Deal / watchlist / scan intents cost a second LLM call only when the primary returns text-only. Typical path stays single-call.
- No keyword list to maintain for intent routing.
