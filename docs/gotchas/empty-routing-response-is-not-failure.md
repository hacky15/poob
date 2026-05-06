---
type: gotcha
status: active
date: 2026-05-06
tags: [brain, llm, gpt-oss, refusal, routing]
related: [[text-casual-fallback-bypass-deal-agent]] [[poobbrain-architecture]] [[groq-gpt-oss-20b-swap]]
---

# An empty completion from a tool-routing LLM is a soft refusal, not a failure

## Trigger

A tool-routing LLM call returns:

- **No exception**
- **No tool call**
- **Empty `content` string**

You're tempted to treat this as "the model didn't have anything to say, fall through to the next provider / agent." That is wrong for RLHF-aligned models and silently sends user input to the wrong code path.

## Why it happens

`openai/gpt-oss-20b` and `gpt-oss-120b` (and other heavily-aligned OpenAI open-weight models) have multiple refusal modes baked in by RLHF training:

1. **Hard refusal**: `"I'm sorry, but I can't help with that."`
2. **Educational deflection**: paragraph-long explanation of why the topic is sensitive.
3. **Silent refusal**: empty `content` string, no tool call, HTTP 200, no error metadata.

Mode #3 is the trap. The model is *successfully* refusing — it just refuses by saying nothing. The HTTP layer sees a clean 200 with empty content and the SDK returns a populated response object. There's no exception to catch and no error code to inspect.

A bench at 2026-05-06 against the live system prompt confirmed gpt-oss-20b returning empty on 4 of 10 horniness=10 prompts (`"would you smash dyno"`, `"yo whats good"`, `"its been soon do you feel differently about it"`, `"Poob I think I want to sex with you"`). Same prompts on llama-3.1-8b-instant produce content; same prompts on qwen3-32b produce content.

## Don't

- **Don't treat empty content + no tool as "Groq is down."** That was the bug at [[text-casual-fallback-bypass-deal-agent]] — the brain logged `poob.groq_down_deal_fallback` and ran the deal sub-agent on casual chat, which then refused with safety boilerplate. Eight refusals in one production session traced to this exact misclassification.
- **Don't add refusal-text regex matching as the detection.** Brittle, locale-dependent, ages badly. The structural "empty + no tool" check is exhaustive: any soft refusal mode looks like that from outside.
- **Don't assume the routing model's text response is suitable for the user.** Even when it returns text, RLHF models often return text that's a refusal in disguise. Use the routing call **only for tool detection**; generate user-facing content with a separate, non-RLHF-heavy model.

## Do

Two things, in order:

1. **Use the routing model only for tool-detection.** When it returns no tool, ignore its text response (or at least don't trust it as your final answer for content the user actually asked for).
2. **Fall through to a casual-content model for any "no tool" case** — empty *or* text. The voice path's pattern is the canonical example: discard routing's text, run a separate streaming call to a less-aligned content model (`llama-3.1-8b-instant`).

The text path now mirrors voice via `_casual_text_fallback` ([poob.py:962-1003](../../src/poob/brain/poob.py#L962-L1003)). Empty content + no tool → casual call to `llama-3.1-8b-instant` with the tool-free system prompt. Only fall through to the deal sub-agent if even llama returns empty (rare).

## Reference

- Decision: [[text-casual-fallback-bypass-deal-agent]]
- Bench scripts: `scripts/bench_refusal.py`, `scripts/bench_chat_latency.py`
- Tests: `tests/unit/test_brain_casual_fallback.py`
- Code: `src/poob/brain/poob.py` — see `_casual_text_fallback` and the `respond` wiring at lines 692–712.
