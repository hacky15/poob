---
type: gotcha
status: active
date: 2026-04-22
tags: [brain, llm, tool-calling, prompt-construction]
related: [[music-tool-hallucination]] [[poobbrain-architecture]]
---

# LLM tool calls whose arguments come from passive context, not the current turn

## Trigger

A tool-routing prompt that includes both (a) the current user message and (b) recent passive chatter (channel history, voice transcript). Small-to-mid models pull tool arguments from the passive context rather than the current turn when any topic-matching token appears there.

## Why it happens

Small tool-calling models weight the most-frequent tokens in their context window. If "Bad Guy" appeared in a passive line 20 minutes ago, and the model sees an even weak music-adjacent signal in the current turn, it fires `music_assistant(query="bad guy by Billie Eilish")` — confident, wrong.

System prompts that push toward tool use ("you MUST call music_assistant for ANYTHING music-related") amplify this — they don't distinguish "signal in the current turn" from "signal anywhere in the prompt."

## Don't

- Strip passive context from the prompt. It's load-bearing for "skip the song" / "who said that thing about X" / contextual replies.
- Trust the LLM's tool-call arguments without checking them against the current user turn.
- Add more "ONLY ACT ON THE CURRENT TURN" instructions to the system prompt. They help a little, but small models still over-fire.

## Do

Post-validate tool-call arguments against the current user transcript before dispatching. For music `play`:

1. Tokenize both the tool-call `query` and the current user message (lowercase, strip punctuation).
2. Strip a small stopword list (`the`, `a`, `an`, `by`, `play`, `song`, `music`, `track`, and the small set of function words the LLM fills queries with when hallucinating).
3. Require at least one non-stopword token from `query` to appear in the user message.
4. If no overlap: **re-derive the query straight from the raw message before giving up.** Run `_music_safety_net(original_message, …)` — if the raw message carries play-intent ("play X", "put on X"), use that faithful query (`music.play re-extracted from raw after hallucination drop`) and proceed. Only if the raw message has *no* play-intent (genuine context-pull) do you log `music.play hallucinated from context — drop` and drop: voice returns empty string (no TTS), text returns a brief acknowledgement.

The re-extract step (added 2026-06-01, see [[groq-429-fallback-routed-to-deals]]) matters because degraded routing (Groq 429) makes the model emit stale/hallucinated queries even for a crystal-clear "play tiki tiki" — the old drop-only guard turned those into "I didn't catch a music request there." The guard's job is to reject the LLM's *wrong query*, not to refuse the user's plainly-stated request. Both guards (`_handle_music` text + `_handle_music_voice_streaming`) do this.

Keep the stopword list short. A long list risks masking legitimate short queries ("play Run" by OneRepublic).

This pattern applies to any tool whose arguments come from the user's intent, not from code. Voice addressee, watchlist item names, deal queries — any tool that could legitimately pull from context can also illegitimately pull from context.

## Reference

Incident: [[music-tool-hallucination]]. Fix implementation: `brain/poob.py:_handle_music` post-validation block.
