---
type: architecture
status: active
date: 2026-03-25
tags: [brain, llm, tool-calling]
related: [[voice-architecture]] [[music-player-architecture]] [[brain-routing-audit]]
---

# PoobBrain — unified personality + deal sub-agent

## Purpose

Single entry point for all Poob interactions (text, DMs, voice). Previously text and voice had separate brains with separate personalities, no shared history, and different capabilities (voice-Poob couldn't help with deals). PoobBrain collapses them into one thin router.

## Shape

```
User input (any source)
    │
    ▼
PoobBrain (tiny ~500-char personality prompt + deal_assistant + music_assistant tools)
    │
    ├── No tool called → casual personality response (Groq, ~200ms)
    │
    ├── deal_assistant called → AgentRunner (heavy prompt, 14 tools)
    │                           │
    │                           └── Result wrapped in Poob personality
    │
    └── music_assistant called → MusicCog.handle_music_request
                                 │
                                 └── Toob personality wrap (voice) or plain text (text)
```

## Key design decisions

- **LLM is the router.** No separate intent classifier. Groq's native function-calling decides whether to route to deal or music. The `deal_assistant` tool description covers all deal/shopping requests; `music_assistant` covers all music requests.
- **Deal agent has no personality** (`personality=False`). `AgentRunner`'s system prompt uses `_SUB_AGENT_PREAMBLE` — clean, functional, no humor. PoobBrain wraps the response in personality after.
- **Data-heavy passthrough.** When the deal agent returns structured data (>500 chars, contains newlines — tables/lists), PoobBrain passes it through without personality wrapping to preserve formatting.
- **Multi-turn deal sessions.** `_deal_context` dict tracks active deal sessions per user. When the deal agent asks a follow-up (response contains "?"), context is preserved so subsequent answers route back to the deal agent.
- **Voice streaming.** `respond_streaming()` checks for tool calls first (non-streaming, fast), then streams the personality wrap sentence-by-sentence for TTS.
- **Shared conversation history.** PoobBrain maintains one in-memory history per user, shared across text and voice. The deal agent maintains its own DB-backed history for tool-calling continuity.
- **Channel context for text channels.** `AgentMessageHandler` fetches ~15 messages from the Discord channel via `channel.history()` and formats them as an attributed transcript (same `[Recent conversation you've been listening to:\n...]` block voice uses). Injected into the *current* LLM turn only — `_split_context()` separates the context block from the clean user text before storing in history, preventing history bloat.

## Latency profile

- Casual text: ~200-400ms (Groq single call, no tools)
- Casual voice: ~500ms total (STT + Groq + TTS)
- Deal text: ~1-3s (Groq routing + deal agent + personality wrap)
- Deal voice: ~2-4s (STT + Groq routing + deal agent + streaming wrap + TTS)

## Key files

- [brain/poob.py](../../src/poob/brain/poob.py) — unified personality layer, tool definitions, `respond` + `respond_streaming`
- [agent/prompts.py](../../src/poob/agent/prompts.py) — `_SUB_AGENT_PREAMBLE`, `personality` param on `build_system_prompt()`
- [agent/runner.py](../../src/poob/agent/runner.py) — `personality` param (defaults True for backward compat)
- [discord_bot/agent_handler.py](../../src/poob/discord_bot/agent_handler.py) — routes through PoobBrain
- [voice/session.py](../../src/poob/voice/session.py) — `brain: PoobBrain` used by the voice session

## Invariants

- **One history per user**, shared across input modalities. If two handlers write concurrently, history gets polluted — see [[one-handler-discord]] for the race that exposed this.
- **Channel context is ephemeral.** Never persisted into history; `_split_context()` strips it before saving. Violating this balloons token usage across turns.
- **Deal agent has no personality prompt.** Adding one produces double-personality output (weird). Personality lives at the PoobBrain layer.
- **Data-heavy responses bypass personality wrap.** Tables, lists, deal details lose formatting through an LLM wrap. The >500-char / newline heuristic gates this.

## Related decisions

- [[brain-routing-audit]] — why the keyword-intent bandaid is gone.
- [[one-handler-discord]] — why only one listener per input modality.
- [[voice-architecture]] — voice session integration.
- [[music-player-architecture]] — music tool routing.
