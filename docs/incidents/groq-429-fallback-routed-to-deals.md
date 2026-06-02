---
type: incident
status: resolved
date: 2026-06-01
tags: [brain, llm, groq, rate-limit, deals, music, routing, persona]
related: [[text-mode-rlhf-refusal-leak-2026-05-29]] [[empty-routing-response-is-not-failure]] [[tool-hallucination-from-passive-context]] [[text-casual-fallback-bypass-deal-agent]] [[poobbrain-architecture]] [[feedback_poob_must_not_refuse]]
---

# Groq 429 fell back to the deal agent — Poob volunteered deals on music/casual turns

## Symptom

Operator, live (two separate turns in one session): *"there's absolutely no reason why it should have mentioned deals in the recent chats. Notice twice."* — and the standing directive that followed: **"deals and marketplaces should ONLY ever be mentioned if it's what the user wants deliberately."**

Concretely, in the voice/text logs:

- "play panda desiigner" → Poob surfaced marketplace/deal talk instead of playing music.
- "normal volume" → same: a volume command produced a deal-flavored reply.
- "play tiki tiki" → *"I didn't catch a music request there."* — a clear play request dropped. Operator: *"after me clearly asking for music 'play tiki tiki'. This cannot be happening."*

All three happened during heavy multi-user voice, when Groq's free tier (30 RPM) was being rate-limited (clusters of `429`/ratelimit in the voice log).

## Root cause

Two distinct defects, both triggered by degraded routing (Groq `429`), both in [poob.py](../../src/poob/brain/poob.py):

**1. Deal agent was the catch-all fallback.** `respond()` wrapped tool-routing in `try/except`. When `_groq_with_tools` raised (e.g. 429) or produced nothing usable, the `except`/fallthrough block ran the **deal agent** (`poob.groq_down_deal_fallback` → `_handle_deal`). So any message — "play panda desiigner", "normal volume", casual chat — that happened to hit a rate-limited router got answered by the deal sub-agent, which talks about marketplace/listings. This directly violated the "deals only when deliberately requested" contract: deals were running as the *degraded-mode default*, not on explicit `deal_assistant` routing. (The voice path `respond_streaming` never had this — it only runs deals on explicit `deal_assistant`, falling to casual otherwise. The text path was the asymmetric defect, same shape as [[text-mode-rlhf-refusal-leak-2026-05-29]].)

**2. Hallucination guard dropped clear "play X" under degraded routing.** The `_handle_music` guard ([[tool-hallucination-from-passive-context]]) rejects a play query whose tokens don't overlap the current message — a defense against the LLM pulling a song title from passive conversation context. Under degraded routing the model emitted a stale/hallucinated query (e.g. a song from earlier context) for "play tiki tiki"; the guard correctly saw no overlap and **dropped to "I didn't catch a music request there."** The guard was right that the *LLM's query* was bad, but wrong to give up — the raw message plainly said what to play.

## Fix

**1. Deals never as fallback.** Removed the deal-agent catch-all from `respond()`. When routing fails or yields nothing, the new order is: (a) `_music_safety_net(clean_message, …)` on the **raw** message — if it carries play-intent ("play X", "put on X", …), route to `_handle_music` with the faithfully-extracted query (`poob.groq_down_music_safety_net`); (b) otherwise `_casual_text_fallback` (`llama-3.1-8b-instant`, non-RLHF); (c) last-resort `_fallback_generate`. The deal agent now runs **only** when the router explicitly picks `deal_assistant` — matching the operator directive.

**2. Guard re-extracts instead of dropping.** Both hallucination guards (`_handle_music` text path + `_handle_music_voice_streaming`) now, on a no-overlap query, re-derive the query straight from the raw message via `_music_safety_net`. If the raw message carries play-intent, the faithful query replaces the hallucinated one (`music.play re-extracted from raw after hallucination drop`) and playback proceeds. If the raw message has *no* play-intent (genuine context-pull, e.g. "how's the weather" + a stale query), the guard still drops — re-extraction is a backstop for clear "play X", not a way to play random context. This **strengthens** the guard (still rejects the LLM's wrong query) while honoring the user's actual words.

Both fixes live at the brain layer, source-side, no text-regex band-aids — the safety net's intent detection is the sanctioned mechanism (already used on the happy path at [poob.py:828](../../src/poob/brain/poob.py)).

## Validation

- `tests/unit/test_brain_casual_fallback.py`:
  - `test_casual_empty_does_not_fall_to_deal_agent` (replaces the old `…_falls_through_to_deal_agent`, which encoded the now-wrong behavior) — casual-empty → `_fallback_generate`, deal agent NOT called.
  - `test_groq_failure_casual_routes_casual_not_deal` — Groq raises on casual → casual fallback, no deal agent.
  - `test_groq_failure_music_request_routes_music_not_deal` — Groq raises on "play tiki tiki" → music via safety net, no deal agent.
  - `test_hallucinated_query_reextracted_from_raw_message` — hallucinated `{query: "panda desiigner"}` + raw "play tiki tiki" → handler receives `query="tiki tiki"`, no "didn't catch".
  - `test_hallucinated_query_still_dropped_when_no_play_intent` — hallucinated query + "how's the weather" → still drops, handler not called.
- Full brain + boob + music-handler suites green (77).
- Watch in prod: under 429 clusters, `poob.groq_down_music_safety_net` and `music.play re-extracted…` should appear; `poob.groq_down_deal_fallback` is gone; no marketplace talk on music/volume/casual turns.

## Follow-ups

- **Root capacity issue not addressed here.** Groq free-tier 30 RPM exhaustion during heavy multi-user voice is the underlying trigger; this fix makes degradation *graceful* (music + casual still work, no deal leak) but doesn't add headroom. A paid tier or a second key is a $0-budget / operator decision, deferred — not assumed.
- The `_music_safety_net` play-signal list is the single source of "what counts as play-intent" for both the happy path and these two fallbacks; extend it there if new phrasings show up, not per-call-site.
