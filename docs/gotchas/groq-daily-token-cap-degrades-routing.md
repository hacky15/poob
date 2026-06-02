---
type: gotcha
status: active
date: 2026-06-02
tags: [brain, groq, rate-limit, routing, capacity]
related: [[brain-routing-audit]] [[music-routing-prompt-thoughtfulness]] [[groq-429-fallback-routed-to-deals]] [[groq-gpt-oss-20b-swap]] [[voice-music-common-pitfalls]]
---

# Groq's per-DAY token cap silently degrades routing mid-session

## Trigger

A long or busy multi-user voice session. Tool routing quality drops partway through and stays degraded for the rest of the calendar day — clear "play X" requests get missed, casual replies feel off.

## What's happening

Groq's free tier caps the **primary router model** (`openai/gpt-oss-20b`) at **200,000 tokens per DAY** (TPD), not just per-minute. A 24h prod audit (2026-06-02) caught it maxed:

```
Error code: 429 … tokens per day (TPD): Limit 200000, Used 199564 … try again in 15m44s
```

71 "Tool detection failed" events in 24h. Once the daily cap is hit, **every** routing call falls through to the next provider in the cascade (NVIDIA `qwen3-next-80b`) for the rest of the day. NVIDIA is a capable fallback but weaker at music tool-calling — most of that day's routing misses (8 missed plays, 3 query hallucinations) clustered while it was carrying routing.

This is distinct from the per-minute (RPM) 429s, which clear in seconds. The daily cap clears only at Groq's reset.

## Don't

- Don't read "routing got worse tonight" as a code regression — check for `tokens per day (TPD)` 429s first (`scripts/logs.sh poob --since 24h | grep "tokens per day"`).
- Don't try to fix routing quality purely in code when the real cause is the primary model being unavailable for the day.
- Don't paper over it by making `_music_safety_net` (the hardcoded "play " matcher) do more — that's the brittle path the operator rejected.

## Do

- **Code side (done):** the cascade degrades gracefully — NVIDIA carries routing, the prompt is tuned so the fallback classifies better ([[music-routing-prompt-thoughtfulness]]), and 429s never fall to the deal agent ([[groq-429-fallback-routed-to-deals]]). This is the $0, in-our-control mitigation.
- **Capacity side (operator decision, $0 default):** more daily headroom needs one of — a second Groq API key, a paid Groq tier, or accepting the NVIDIA-fallback quality after the cap. Per the project's $0-spend default, this is surfaced to the operator, not chosen autonomously.
- The last-resort cascade rung (Groq `llama-4-scout-17b`) is a *different* Groq model with its own separate daily budget, but it over-routes to music ([[voice-music-common-pitfalls]]) so it stays last.

## Reference

Cascade order and rationale: [[brain-routing-audit]], [[groq-gpt-oss-20b-swap]]. The day this was quantified: see [[music-routing-prompt-thoughtfulness]] (24h telemetry).
