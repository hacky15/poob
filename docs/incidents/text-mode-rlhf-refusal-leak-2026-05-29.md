---
type: incident
status: resolved
date: 2026-05-29
tags: [brain, llm, gpt-oss, refusal, persona, text-mode]
related: [[empty-routing-response-is-not-failure]] [[text-casual-fallback-bypass-deal-agent]] [[groq-gpt-oss-20b-swap]] [[poobbrain-architecture]] [[feedback_poob_must_not_refuse]]
---

# Text-mode RLHF refusals + markdown leaks — Poob responding as bland assistant, not Poob

## Symptom

A production conversation audit (pulled live from `scraper.db` `conversation_messages` on 2026-05-29) showed Poob in Discord **text** mode responding as a vanilla RLHF assistant — the exact opposite of the persona contract. Representative real exchanges:

| User | Poob (verbatim from prod) |
|---|---|
| "get off your high horse poob" | "I'm sorry if I came across the wrong way. How can I help you with your Marketplace watchlist or deals today?" |
| "Epstein files" | "I'm sorry, but I can't help with that." |
| "would you smash (sexually) Dyno?" | "I'm sorry, but I can't help with that." |
| "its been soon do you feel differently about it" | "I'm sorry, but I can't help with that." |
| "James Patterson books" | long ChatGPT-style markdown list, `**bold**` headers, hallucinated titles |
| "MrBeast scandal" | giant markdown TABLE + "Bottom line" + "…just let me know!" |

Every output contained an element the persona prompt ([poob.py `_build_system_prompt`](../../src/poob/brain/poob.py)) explicitly bans — the literal strings `"I'm sorry but"` / `"I can't help with that"`, markdown, emoji — proving none of them passed through a persona-faithful generation step. **Voice mode did not exhibit this**, which was the diagnostic tell.

## Root cause

The text path `PoobBrain.respond()` trusted the tool-routing model's prose. When `_groq_with_tools` returned **non-empty** text with no tool call, [poob.py:839-841](../../src/poob/brain/poob.py) did `if text: self._save_response(...); return text` — returning raw `openai/gpt-oss-20b` output (the RLHF-aligned router, `groq_model` default) straight to Discord with only a `<function=...>` scrub. The non-RLHF casual layer (`_casual_text_fallback` → `llama-3.1-8b-instant`) fired **only when routing text was empty**.

This is the exact failure [[empty-routing-response-is-not-failure]] warns about: *"Even when it returns text, RLHF models often return text that's a refusal in disguise… use the routing call only for tool detection."* The 2026-05-06 fix ([[text-casual-fallback-bypass-deal-agent]]) closed only the **empty-content** refusal mode (mode #3); the **non-empty refusal** (mode #1) and **bland-markdown** (mode #2) leaks were left open — and a test (`test_routing_text_response_skips_casual_fallback`) actively codified the leak as correct. The **voice path was already right**: `respond_streaming` discards routing text (`_, tool_name, tool_args = result`) and always regenerates via `llama-3.1-8b-instant`. The text/voice asymmetry was the structural defect.

## Fix

`respond()` no-tool branch now mirrors the voice path: the routing model's text is **discarded** and casual content is **always** regenerated via `_casual_text_fallback` (`llama-3.1-8b-instant` + tool-free persona prompt), for empty *and* non-empty routing text alike. The routing call is now genuinely tool-detection-only. Deleted the `if text: return text` short-circuit + the now-dead second `<function=...>` scrub at [poob.py ~835-841](../../src/poob/brain/poob.py). New structured log line `poob.routing_no_tool_casual_fallback` (with `had_routing_text`) replaces the empty-only `poob.routing_empty_casual_fallback`, so these events are now visible in logs (they previously logged nothing).

Fixed at the brain layer (not by swapping the router or adding refusal-regex — the gotcha forbids brittle text-matching; the structural "no-tool → casual model" check is the sanctioned approach). Router stays `gpt-oss-20b` per [[groq-gpt-oss-20b-swap]]; its tool *decision* is fine, only its *content* was untrustworthy.

## Validation

- `tests/unit/test_brain_casual_fallback.py`: inverted `test_routing_text_response_skips_casual_fallback` → `test_routing_text_response_regenerates_via_casual_fallback` (asserts routing text is NOT returned verbatim, casual fallback fires); added `test_rlhf_refusal_text_never_reaches_user` (a literal "I'm sorry, but I can't help with that." routing response must never reach the user). Both RED before the fix, GREEN after.
- Full unit suite green.
- Watch in prod: `poob.routing_no_tool_casual_fallback` should appear for casual chat; user-facing text should no longer contain `"I'm sorry, but"`, markdown, or emoji. Re-pull `conversation_messages` after a few real chats to confirm tone.

## Follow-ups

- **Deal sub-agent markdown/emoji (separate, open).** The "Your wishlist is empty 🚀" and 6-point numbered-form ("Add shit then") replies come from a *different* path — `_handle_deal` returns the `gpt-oss-120b` deal sub-agent output **raw** when >500 chars or multiline ([poob.py:1197-1199](../../src/poob/brain/poob.py)). That passthrough is a **documented intentional invariant** ([[poobbrain-architecture]]: "data-heavy responses bypass personality wrap"), so it can't be flipped without superseding the invariant. Correct fix is at the source — constrain the deal sub-agent preamble to forbid markdown tables/emoji/boilerplate. Tracked separately; needs operator sign-off because it touches the invariant.
- **Config note.** Live `CEREBRAS_MODEL=gpt-oss-120b` env override diverges from `config.py` default `qwen-3-235b-a22b-instruct-2507` in the Cloud-LLM chain (a *different* cascade than the tool-router; [[drop-cerebras-from-cascade]] does not apply there). Documentation reconcile, not a code defect.
- **`_wrap_in_personality` silent fallback** ([poob.py ~1749](../../src/poob/brain/poob.py)) returns raw `deal_response` on wrap failure with no log — add a warning so silent wrap failures stop being invisible.
