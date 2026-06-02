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

- **Primary mitigation (done 2026-06-02):** a **Gemini Flash-Lite rung** now sits between Groq and NVIDIA in the tool cascade ([[gemini-tool-router-rung]]). Gemini's free tier is RPD-limited with **no daily token cap**, so when Groq's per-day token cap is spent, Gemini — not the weaker NVIDIA rung — carries routing (benchmarked 8/8 correct at ~700 ms on our real tool defs). This directly closes the degradation window at $0, no card.
- **Prompt side (done):** the prompt is tuned so every cascade model classifies better ([[music-routing-prompt-thoughtfulness]]), and 429s never fall to the deal agent ([[groq-429-fallback-routed-to-deals]]).
- **Local floor (NOT viable — CPU-only box):** self-hosted Qwen3 (qwen3:4b/8b, already pulled) would be an unlimited terminal rung in principle, BUT benchmarked at **50–120 s per call** here (qwen3:8b times out). The homelab is **CPU-only — no GPU** (operator-confirmed 2026-06-02), so that latency is the hardware floor, not a fixable passthrough issue. Local is off the table for real-time routing for good; don't re-propose it. (Fine for latency-tolerant tasks only.)
- **Capacity side (operator, $0 default):** Gemini's ~1,000 RPD is shared per-project; if it too gets exhausted, more headroom needs a second Groq key, a different Gemini model slot for the router, or accepting NVIDIA quality. Not chosen autonomously.
- The last-resort cascade rung (Groq `llama-4-scout-17b`) is a *different* Groq model with its own separate daily budget, but it over-routes to music ([[voice-music-common-pitfalls]]) so it stays last.

## Reference

Cascade order and rationale: [[brain-routing-audit]], [[groq-gpt-oss-20b-swap]]. The day this was quantified: see [[music-routing-prompt-thoughtfulness]] (24h telemetry).
