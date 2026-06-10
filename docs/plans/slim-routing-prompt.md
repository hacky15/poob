---
type: plan
status: proposed
date: 2026-06-10
tags: [brain, llm, routing, latency, prompt, voice]
related: [[cascade-outage-nvidia-hang-gemini-rpm]] [[free-llm-tier-audit-2026-06]] [[gemini-tool-router-rung]] [[groq-daily-cap-routing-storm]] [[text-casual-fallback-bypass-deal-agent]]
---

# Plan: lossless slim routing prompt

Pre-coding plan from a 32-agent read-only audit of `src/poob/brain/poob.py` (inventory → adversarial-refute every candidate cut → synthesize). Goal: give the ROUTING call a focused classifier prompt without losing ANY routing functionality.

## Verified architecture (the thing that makes this lossless)

The routing call and the casual reply are **separate LLM calls**. The routing call (`_groq_with_tools`) emits a tool decision; its **text is discarded** — `respond_streaming` does `_, tool_name, tool_args = result` (poob.py:1024), `respond` "discards the routing text and regenerates casual" (poob.py:937). The casual reply is regenerated with the **full personality prompt** via `_rebuild_messages_no_tools` → `llama-3.1-8b-instant` (poob.py:1101). So persona / RULES / NEVER / VOICE text is **dead weight on the routing call** — it shapes text that gets thrown away, and it still runs in casual generation.

## Honest correction to the earlier framing

The "~4k-token routing prompt → slim to ~1k → Groq lasts 3× longer" claim was **wrong**. **Measured (tiktoken cl100k_base, offline):** the full routing system prompt is **1,338 tokens**, the slim prompt is **720** → **618 tokens saved per routing call**. The two tool JSON schemas are **1,845 tokens** and are **KEPT** in both. So the full prompt+schemas payload is ~3,183 tokens (+ history/context ≈ the `Requested 4006` in the Groq 429); slimming removes **~19% of the prompt+schemas input**. That is roughly **~59 Groq turns/day instead of ~50 — not 150.** The text/DM `with_music=False` case is far smaller (339 chars) since the whole music block drops. This change is therefore primarily an **accuracy + correctness-hygiene** win (focused classifier + closing real test gaps), with a modest latency/token benefit. It does **not** by itself make Groq-primary sustainable or kill the daily-cap cliff — the bigger token mass is the tool schemas (1,845 tok), which are the routing decision space and stay.

## What gets cut — and what does NOT

**dedup_to_schema is EMPTY.** The adversarial pass refused every "delete because the tool schema covers it" candidate. Every routing rule and example stays:
- Dual-surfaced rules (music-vs-HEAR prompt 203-205 ↔ MUSIC_TOOL.description 262-266; effect maps 182-190 ↔ effect param 376-390) are dual-surfaced **on purpose** per [[music-tool-hallucination]] — tests assert both copies.
- Prompt-only fragments with no schema equivalent: deal "only when explicitly asked" (173), "whatever follows play IS the song / nonsense name" (178-181), bare-`normal` defusing guard (186-190), volume-vs-effect "NEVER apply_effect" contrast (191-196), the NOT-music negatives (206-211), the "just call it" anti-fabrication clause (174-177).

**Moved to generation-only (still runs on casual + wrap paths, dropped from routing):** persona intro (127-130), vibe/horniness (130-131), RULES block (135-157), NEVER block (158-168), VOICE block (216-222).

## Approach (avoids the multi-caller hazard)

Add a NEW `_build_routing_prompt(voice: bool, with_music: bool) -> str`. Call it from `_build_messages` ONLY for the routing path (replacing the `_build_system_prompt(level, voice=voice)` at line 1195 for routing). Leave `_build_system_prompt` **untouched** — the casual path (`_rebuild_messages_no_tools`) and the two personality-wrap generators (1875/1922, which default `with_tools=True`) keep calling it, so all persona/markdown-ban/voice text is preserved for generation. The ACTIVE DEAL SESSION hint (1197-1204) and the `_music_context_block` (1206-1209, with the literal `MUSIC IS CURRENTLY PLAYING` marker) are appended by `_build_messages` unchanged. `with_music=False` (text/DM, no music handler — mirrors the conditional MUSIC_TOOL wiring at 1978-1980) omits the music block entirely.

Draft routing-prompt text: a 2-sentence classifier framing replacing the persona intro, then poob.py lines 173-213 **verbatim**, with "answer these YOURSELF" / "never respond with text" reworded to "return no tool call" so it reads as routing instruction. Full draft stored with the audit run.

## Tests FIRST (TDD — 15 tests, several close genuine pre-existing gaps)

Safety proof + move-guards:
- `test_routing_invariance_persona_removed` — **the core proof**: tool decision identical old-prompt vs new-prompt across `['slow it down and reverb','skip','play tiki tiki','what is on my wishlist','normal volume','flip a coin','play some fuckin nightcore','would you smash dyno']`, run through real `_groq_with_tools` logic.
- `test_routing_prompt_omits_persona_and_rules` / `test_routing_prompt_keeps_all_music_routing_rules` / `test_routing_prompt_drops_music_block_when_no_handler`.
- `test_casual_generation_prompt_still_carries_persona_and_rules` / `test_voice_generation_prompt_still_carries_voice_block` — guard the MOVE (every clause the historian flagged NEEDS-TEST-FIRST).
- `test_active_deal_session_hint_survives_into_routing`, `test_music_context_block_marker_string_preserved`, `test_gemini_routing_uses_routing_prompt_shape`.

Historian-flagged **already-untested routing rules** (worth tests regardless of this change):
- `test_deal_side_tool_worthy_gate_continues_cascade` (HIGH — the music half is tested, the deal half is NOT).
- `test_music_safety_net_play_signal_matching`, `test_scrub_music_query_happy_path`, `test_empty_short_query_gate`, `test_tool_detection_token_floor` (the `_TOOL_DETECTION_MAX_TOKENS=256` floor at line 2070), `test_deal_intent_positive_routing`.

## Before/after live benchmark (gates the merge)

`tests/manual/bench_routing.py` (not in the unit suite — hits live providers): real PoobBrain + production cascade + stub MusicHandler; call `_groq_with_tools(messages, max_tokens=80)` directly; 7 utterances (the invariance set), 5× each, record median+p95 latency, modal tool decision, and input-token count (validate the ~490–535 claim empirically). PASS = (a) zero routing-decision flips before→after, (b) after median ≤ before on every utterance, (c) **crude request #7 must NOT regress route→no-tool**.

## Open risks

1. **Residual crude-routing risk** (the only cut not provably zero): dropping be-crude/anti-refusal text from routing *could* let a refusal-prone rung decline to emit a tool on an edgy-but-tool-warranting request. Vault evidence: RLHF refusal triggers on topic detection in weights, not instruction count ([[text-casual-fallback-bypass-deal-agent]]); a no-tool result is structurally caught by casual fallback. **Benchmark #7 is the gate** — if it regresses, revert the crude-clause cut.
2. **String-contract coupling**: `MUSIC IS CURRENTLY PLAYING` (poob.py:835) is hard-scanned by the `control_signals` gate (2049) + gemini-router tests. Append `_music_context_block` unchanged; do NOT touch `_groq_with_tools`. Tidying the marker silently kills the gate → reproduces [[cascade-outage-nvidia-hang-gemini-rpm]].
3. **Multi-caller hazard**: `_build_system_prompt(with_tools=True)` is also the default for the wrap generators (1875/1922). Use a NEW builder; do NOT gate the existing function, or the wraps lose persona + markdown ban.
