---
type: decision
status: active
date: 2026-06-02
tags: [brain, music, llm-tool-calling, routing, prompt]
related: [[brain-routing-audit]] [[music-tool-hallucination]] [[tool-hallucination-from-passive-context]] [[groq-429-fallback-routed-to-deals]] [[groq-daily-token-cap-degrades-routing]] [[poobbrain-architecture]]
---

# Music routing thoughtfulness — fix the prompt, not a hardcoded play==music rule

## Context

Operator, live: *"sometimes it feels like it needs to be more thoughtful of song requests. Why would it not play it when I typed 'play'. Address this cleanly: don't just hardcode 'play' to be music. Fix the prompt or whatever backend logic to ensure no false positives or negatives."*

A 24h production log audit (voice/brain stream, separated from scraper) quantified it. Of ~46 music interactions:

- **35** routed correctly by the LLM (19 Groq gpt-oss-20b, 16 NVIDIA qwen3-next-80b).
- **8** the LLM **missed entirely** — caught only by the hardcoded `_music_safety_net` "play " prefix matcher: "cheeky cheeky", "body dee dum", "starships", "hoodlum trap phonk music", "tiki tiki" ×2, "sigma song", "sigma phonk". All genuine requests.
- **3** the LLM routed `play` with a query **hallucinated from stale context** ("play starships" → query "panda", "play tiki tiki" → query "panda") → dropped by the post-validation guard. (Plus one correct drop: "Bring it." → "Cheeky…", a non-music turn.)

~24% LLM mishandle rate on music. Two failure modes: under-routing clear "play X", and hallucinating the query from passive context. No false-positive (non-music "play") was observed, but the existing brittle safety net *would* fire on "let's play a game" → play "a game".

## Decision

Fix at the **prompt / tool-description layer** — make the model classify thoughtfully — rather than hardcoding `"play" == music` in code (which the operator rejected, and which [[brain-routing-audit]] already flags as fragile). Two edits in `brain/poob.py`:

### A. System-prompt `with_tools` music block ([poob.py `_build_system_prompt`](../../src/poob/brain/poob.py))

- **Replaced** the over-aggressive *"If the user says ANYTHING that could be a request to play … you MUST call music_assistant"* — [[music-tool-hallucination]] identified that exact wording as *compounding* context-hallucination. Now scoped to "when the CURRENT message asks to play/queue/control music, call it."
- **Added positive nonsense-name guidance** (the false-negative fix): *"Whatever follows play/put on/queue IS the song — even if it sounds like nonsense"*, with examples `play cheeky cheeky`, `play tiki tiki` — the literal misses from the logs.
- **Added negative examples** (the false-positive guard): `let's play a game`, `good play`, `play it cool` → NOT music. "'play' means music ONLY when they're asking to hear a song, artist, or genre."
- **Added current-turn scoping**: take the song from THIS message only, never from earlier turns or others' speech.

### B. `MUSIC_TOOL.query` description

Extract the song from the user's **CURRENT message** only; **never** a title from earlier in the conversation or from what others said; if no song is named, don't borrow one from context. Attacks the hallucination at the tool-def source, complementing the existing post-validation guard ([[music-tool-hallucination]]) and the raw-message re-extract ([[groq-429-fallback-routed-to-deals]]).

## What was deliberately NOT changed

- **`tool_signals` cascade-retry gate stayed deal-only.** [[brain-routing-audit]] says expand it only when tool-miss telemetry justifies the cost — the telemetry now does, BUT expanding it with music keywords is the exact hardcode the operator rejected, and unconditional retry would push casual chat into Scout's over-routing (a new false-positive source per [[voice-music-common-pitfalls]]). The prompt lever improves every cascade model without a keyword list.
- **`_music_safety_net` kept as a backstop, not invested in.** It's the "play " hardcode the operator dislikes; the prompt fix shrinks its load. If post-deploy telemetry shows the LLM now routes reliably, retiring/tightening it is a clean follow-up. Not removed now — that would regress the 8 catches until the prompt proves out.

## Why prompt-only is honest, not a half-measure

The real reliability ceiling is capacity: Groq's gpt-oss-20b hits its **200k tokens/day** cap mid-session ([[groq-daily-token-cap-degrades-routing]]), forcing routing onto the weaker NVIDIA fallback for the rest of the day — that's where most misses cluster. The prompt fix is the $0, in-our-control lever that improves NVIDIA's adherence. More token headroom (second key / paid tier) is an operator decision and is surfaced separately, not assumed.

## Validation

- `tests/unit/test_brain_casual_fallback.py`: `test_music_prompt_has_negative_examples`, `test_music_prompt_current_turn_only`, `test_music_prompt_nonsense_name_is_the_song`, `test_music_tool_query_description_is_current_turn_only`, `test_music_prompt_drops_anything_music_adjacent_overreach` (guards the over-aggressive wording from returning). Prompt-content regression guards in the established `test_system_prompt_*` style.
- Full unit suite green.
- **Exit criterion (post-deploy telemetry):** `Safety net caught missed music intent` and `music.play hallucinated from context — drop` counts drop materially from the 24h baseline (8 and 3); no false-positive plays (a music.action `play` for a non-music "play"). Re-pull the voice stream after a session to confirm.

## Rollback

Revert the two prompt/tool-def edits in `_build_system_prompt` and `MUSIC_TOOL`. Routing reverts to the prior aggressive wording; misses and hallucinations return to baseline.
