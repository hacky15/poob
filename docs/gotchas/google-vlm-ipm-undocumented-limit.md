---
type: gotcha
status: active
date: 2026-03-25
tags: [vlm, google, gemini, rate-limit]
related: [[vlm-cascade-operational-findings]] [[vlm-triage-pipeline]]
---

# Google VLM has an undocumented Images-Per-Minute limit

## Trigger

Bursting VLM calls through Gemini Flash / Flash Lite / Pro from one or more providers. The daily RPM/RPD is nominally fine, but `RESOURCE_EXHAUSTED` starts firing within ~8-30 evals.

## Why it happens

Google AI Studio has an **undocumented Images Per Minute (IPM) limit** on the free tier — approximately 2-10 images/minute, separate from RPM/RPD. VLM calls send 1-2 images each; a burst of 50 listings × 6 images = 300 images, far exceeding IPM. The circuit breaker trips silently and the rest of the cascade (Groq Vision at 14,400 RPD) then gets flooded and rate-limited in turn, leaving only Ollama.

## Don't

- Track only RPM/RPD and assume "free tier is fine."
- Use a token-bucket limiter (allows bursts, exactly the pattern that trips IPM).
- Queue all images back-to-back when a patrol cycle emits many listings at once.

## Do

- Track per-provider IPM separately from RPM/RPD.
- Use a leaky bucket — smooth constant rate, never burst.
- Preemptively throttle at 85% of daily limit.
- Allocate a per-cycle budget: `budget = rpd * 0.80 / expected_daily_cycles`.

This was flagged as "fix needed" rather than implemented; any VLM-cascade refactor should address it before adding more callers.

## Reference

Observed behavior: [[vlm-cascade-operational-findings]] — Gemini Flash hits `RESOURCE_EXHAUSTED` after ~8 evals under current patrol load.
