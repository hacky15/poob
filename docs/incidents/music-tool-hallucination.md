---
type: incident
status: resolved
date: 2026-04-22
tags: [brain, music, llm-hallucination, tool-calling]
related: [[poobbrain-architecture]] [[tool-hallucination-from-passive-context]]
---

# LLM played songs from 20-minute-old passive context

## Symptom

The brain issued `music_assistant(action=play, query=...)` calls for songs the user never requested in their current turn.

Two confirmed incidents, same session:

1. Ben's transcript: `"Oh,"` (wake_word=True). LLM tool-called `play "The Untold by Secession Studios"` — a song requested earlier in the session.
2. Ben's transcript: `"I said, hey, Poob, knock it off with the horniness."` — purely behavioral, zero music intent. LLM tool-called `play "bad guy by Billie Eilish"` — a song requested 20 minutes prior. Other users reacted aloud: *"Two played, bro. Bad guy? Why?"*

## Root cause

The tool-detection prompt includes `[Recent conversation you've been listening to:]` with 15 lines of passive chatter. When a prior music request sits in that context, small tool-calling models (Groq llama-3.3-70b) weight the frequent title tokens and fire `music_assistant` even when the current turn has no music intent.

The system prompt compounds this with: *"If the user says ANYTHING that could be a request to play... you MUST call music_assistant."* — a strong bias toward tool-calling when any music-adjacent token appears anywhere in the prompt.

## Fix

Post-validation in `brain/poob.py:_handle_music`: for `action == "play"` calls, tokenize both the `query` field and the current user message, strip a small stopword list (`the`, `by`, `play`, `song`, etc.), and require at least one meaningful query token to appear in the user's message. No overlap → log `music.play hallucinated from context — drop` and drop. Voice returns empty string (no TTS fires); text gets a brief acknowledgement.

Chosen at this layer because the tool-routing cascade has four providers, all of which can hallucinate under context contamination. A single post-validation step catches all of them. The alternative — stripping music history from the prompt — would cost legitimate contextual routing (e.g. "play something else" after a prior request). Token-overlap preserves that path.

## Stopword list

Kept short on purpose: articles, prepositions, and the words the LLM most commonly fills queries with when hallucinating. Too-large stopword lists would mask legitimate short queries (e.g. "play Run" → "Run" by OneRepublic).

## Validation signal

Post-deploy, look for `music.play hallucinated from context — drop` warnings. Each one is a hallucination caught. A false positive would also log here — review any triggering message that looks like a real request.

## Follow-ups

Captured as a gotcha: [[tool-hallucination-from-passive-context]] — the underlying pattern applies to any tool call whose arguments can be pulled from conversation history rather than the current turn.
