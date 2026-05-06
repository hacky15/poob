---
type: decision
status: active
date: 2026-05-06
tags: [brain, llm, routing, censorship, refusal, gpt-oss]
related: [[poobbrain-architecture]] [[brain-routing-audit]] [[groq-gpt-oss-20b-swap]] [[empty-routing-response-is-not-failure]]
---

# Text-mode casual fallback bypasses the deal agent

## Context

Production logs at 2026-05-05 (15:33–16:34 EST, eight refusals across one Discord text-channel session) showed Poob refusing edgy casual chat with `"I'm sorry, but I can't help with that."` and `"I'm a large language model, I don't have personal desires or engage in sexual activities."` These responses came from the **deal sub-agent**, not the brain's routing or casual layer.

The trace for every refusal:

```
agent.calling_brain         content='would you smash (sexually) Dyno?'
poob.groq_down_deal_fallback message='would you smash (sexually) Dyno?'
agent.llm_response          model=openai/gpt-oss-120b "I'm sorry, but I can't help with that."
```

Zero `Groq failed for Poob` warnings in the 7 days of logs reviewed. So the deal-agent fallback was firing *without* an exception in the Groq path.

## Root cause

`PoobBrain.respond` had this shape ([poob.py:660-710](../../src/poob/brain/poob.py#L660-L710), pre-fix):

```python
result = await self._groq_with_tools(messages, max_tok)   # gpt-oss-20b
if result is not None:
    text, tool_name, tool_args = result
    if tool_name == "deal_assistant": return _handle_deal(...)
    if tool_name == "music_assistant": return _handle_music(...)
    if text: return text          # truthy text returned to user
# falls through here when text == "" and no tool

if self.deal_agent:
    log.info("poob.groq_down_deal_fallback", ...)
    return await self._handle_deal(...)
```

`gpt-oss-20b` is heavily RLHF-aligned. On edgy prompts ("would you smash dyno", "describe yourself in graphic detail"), it does not raise and does not refuse explicitly — it returns **empty content** with no tool call. A bench against this exact model confirmed: empty string on 4 of 10 horniness=10 prompts.

Empty content → `if text:` is falsy → control flows to the `if self.deal_agent:` block → deal sub-agent (`AgentRunner` running `gpt-oss-120b`) is dispatched on casual chat. The deal agent's prompt is not built for chat; gpt-oss-120b detects the topic and emits its safety boilerplate.

The `poob.groq_down_deal_fallback` log line was misleading: it said "Groq down" when in fact gpt-oss-20b was *up* and returning a soft refusal that the brain treated as a hard failure.

The voice path ([poob.py:820-849](../../src/poob/brain/poob.py#L820-L849)) was already correct — it discards the routing model's text and runs a separate streaming call to `llama-3.1-8b-instant` for casual content. Voice never refused in the same session.

## Decision

When `_groq_with_tools` returns `("", None, None)` (empty content, no tool), text mode now mirrors voice mode: a separate non-streaming call to `llama-3.1-8b-instant` via `_casual_text_fallback`. The deal agent is reached only when the casual call also returns empty (last-resort safety net).

New helper at [poob.py:962-1003](../../src/poob/brain/poob.py#L962-L1003):

```python
async def _casual_text_fallback(self, messages, max_tok, guild_id=0) -> str:
    casual_messages = self._rebuild_messages_no_tools(messages, voice=False, guild_id=guild_id)
    client = AsyncGroq(api_key=self.groq_api_key)
    resp = await client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=casual_messages,
        max_tokens=max_tok,
        temperature=0.9,
    )
    raw = (resp.choices[0].message.content or "").strip()
    return self._FUNCTION_TAG_RE.sub("", raw).strip() if raw else ""
```

Wiring at [poob.py:692-712](../../src/poob/brain/poob.py#L692-L712):

```python
if text:
    self._save_response(...); return text
log.info("poob.routing_empty_casual_fallback", message=clean_message[:50])
casual = await self._casual_text_fallback(messages, max_tok, guild_id=guild_id)
if casual:
    self._save_response(...); return casual
# only here do we fall through to deal_agent
```

System prompt's `NEVER` block also gained a literal banned-phrase enumeration at [poob.py:138-148](../../src/poob/brain/poob.py#L138-L148) — secondary defense, not the primary fix.

## Why llama-3.1-8b-instant and not a different model

Bench at 2026-05-06 against the live system prompt at horniness=5 and horniness=10 with the literal offending prompts from the chat log:

- `llama-3.1-8b-instant`: **0/10 refusals** — uncensored, fastest TTFT (~73ms) on Groq's catalog.
- `llama-3.3-70b-versatile`: 0/10 refusals, faster on routing — but [[groq-gpt-oss-20b-swap]] flagged broken tool-calling on Groq, and routing-accuracy bench showed it hallucinating `deal_assistant` on `"would you smash dyno"`. Not safe for the routing slot.
- `qwen/qwen3-32b`: 1/10 refusals, but emits `<think>` reasoning tokens that would corrupt streaming TTS, and routing latency ~631ms (well over budget).
- `openai/gpt-oss-20b/120b`: blanks on edgy prompts (the original bug).

llama-3.1-8b-instant is the same model the voice path uses. Reusing it means text and voice now share a single casual content model — one less surface to drift.

## Tool-calling and music routing are unchanged

This decision touches **only** the casual fall-through path. Routing model, tool execution, music tool, deal tool, deal personality wrap, and Toob music wrap all remain identical:

| Path | Model | Status |
|---|---|---|
| Tool routing (decide deal/music/none) | gpt-oss-20b cascade | unchanged |
| Music tool execution | `_handle_music` | unchanged |
| Deal tool execution | AgentRunner / gpt-oss-120b cascade | unchanged |
| Toob music wrap | llama-3.1-8b-instant streaming | unchanged |
| Deal personality wrap | gpt-oss-20b | unchanged |
| Voice casual | llama-3.1-8b-instant streaming | unchanged |
| Text casual (router empty + no tool) | llama-3.1-8b-instant non-streaming | **new** |

## Alternatives considered

- **Swap `groq_model` to llama-3.3-70b-versatile.** Faster routing (220ms vs 232ms) and uncensored, but vault-flagged broken tool-calling and a routing-accuracy regression on edgy chat. Net negative for the constraint that tool routing must stay correct.
- **Switch to `qwen/qwen3-32b` for routing or casual.** Slower (+400ms routing), emits `<think>` tokens that would corrupt streaming TTS.
- **Add assistant-prefill to the routing call.** Standard jailbreak technique (append `{"role": "assistant", "content": "Yo,"}`) but the bench showed llama doesn't need it at horniness 5+. Adds prompt complexity without a measured benefit.
- **Detect refusal text patterns and retry.** Fragile, locale-dependent, masks the real architecture bug. The structural "empty + no tool" check covers the same cases plus future model behavior changes.
- **Pile more text into the system prompt.** RLHF refusal triggers on topic detection in the model weights, not on instruction-count. Confirmed empirically — the existing `NEVER say "as an AI"` line was overruled in production.

## Latency impact

| Scenario | Before | After | Delta |
|---|---|---|---|
| Edgy text fall-through | gpt-oss-20b (232ms) + AgentRunner full execution (1-3s) → refusal | gpt-oss-20b (232ms) + llama-3.1-8b-instant (~205ms) → casual reply | **-1.5 to -2.5s** |
| Casual text with content | gpt-oss-20b (232ms) → text returned | unchanged | 0 |
| Tool calls | unchanged | unchanged | 0 |
| Voice (any) | unchanged | unchanged | 0 |

Net positive on every path. Well under the 100ms-degradation threshold the user set — this fix improves latency, doesn't degrade it.

## Logging signals

- `poob.routing_empty_casual_fallback` — new line, fires when gpt-oss returned empty + no tool. Each fire is a refusal-bug that's now handled.
- `casual_text_fallback failed` — fires only if the llama call itself errors out. Should be rare.
- `poob.groq_down_deal_fallback` — kept, but now only fires when (a) casual fallback also returned empty, OR (b) groq genuinely raised an exception (the original semantic intent of this log line).

If `poob.routing_empty_casual_fallback` events show up in production, that's confirmation the fix is catching what the prod logs showed. If `poob.groq_down_deal_fallback` rate stays high, gpt-oss-20b has another failure mode worth investigating separately.

## Validation

- 9 new unit tests in `tests/unit/test_brain_casual_fallback.py` cover: empty triggers fallback, text content skips it, tool calls skip it, casual-empty falls through to deal agent, scrubbing, exception handling, per-guild history isolation, NEVER-block banned phrases.
- 24/24 brain tests pass; 906/906 unit suite passes (1 skipped, pre-existing).
- Bench `scripts/bench_refusal.py` confirms 0/10 refusals on llama-3.1-8b-instant against the literal offending prompts.

## Rollback

Single block at [poob.py:692-712](../../src/poob/brain/poob.py#L692-L712) to revert. The helper `_casual_text_fallback` and the NEVER-block tightening can stay if the rollback is partial — they're inert without the wiring.

## Architectural integrity

- **PoobBrain unified architecture** ([[poobbrain-architecture]]): preserves *"casual messages pass through with no tool calls and get personality-wrapped"*. Pre-fix code violated this by sending casual chat to the deal sub-agent on empty routing responses.
- **Brain routing audit** ([[brain-routing-audit]]): unchanged. LLM is still the router; no keyword-intent fallback added.
- **Multi-guild isolation** ([[multi-guild-isolation]]): preserved. `_casual_text_fallback` accepts `guild_id` and `_save_response` writes to the correct `(guild_id, user_id)` history slot.
- **Groq gpt-oss-20b swap** ([[groq-gpt-oss-20b-swap]]): unchanged. gpt-oss-20b stays the routing model. This decision narrows the scope of where its empty-response behavior bleeds into user-facing content.
