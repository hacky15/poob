---
type: incident
status: active
date: 2026-09-06
tags: [brain, llm, routing, rate-limit, cascade, groq, gemini, model-freshness]
related: [[slim-tool-schemas]] [[slim-routing-prompt]] [[cascade-outage-nvidia-hang-gemini-rpm]] [[groq-daily-cap-routing-storm]] [[control-command-misroute-by-weak-rung]] [[voice-llm-model-deprecated-and-never-wired]] [[voice-model-eval-2026-09-06]]
---

# The 8k TPM routing ceiling is the common denominator behind "brain glitched", wrong effects, and misroutes

## Symptom

Operator, after weeks of recurring complaints: *"plenty of 'brain glitched, say
that again', plenty of times where the effects just didn't apply or it took a
while for it to apply, some double queues... NOTHING is fixing them at the
root."*

Census over the full 41-day log (2026-07-27 → 2026-09-06, 129,196 records):

| Failure class | Count |
|---|---|
| "brain glitched, say that again" | 25 |
| Safety-net rescues of a bad/missing LLM route | 37 |
| — of which: misrouted play request | 15 |
| — of which: missed music intent entirely | 18 |
| Routing calls that fell through to the Gemini rung | 49 / 245 (**20%**) |
| Duplicate play within 60s | 2 (both pre-dating the dedup fix; 0 since) |

## Root cause

**Every routing call costs ~3,100 tokens against a Groq free-tier limit of
8,000 tokens per minute.** Measured in the running container:

| Component | Tokens, every call |
|---|---|
| `_build_routing_prompt(with_music=True)` | 1,125 |
| `MUSIC_TOOL` + `DEAL_TOOL` schemas | 1,406 |
| Conversation/passive context | ~500–680 |
| **Total** | **~3,100** |

Corroborated by the 429 bodies themselves: `Limit 8000, Used 5454, Requested
3212`. That is **~2.6 routing calls per minute** before the router hard-fails —
and a voice channel with several people trivially exceeds that.

The degradation chain, every step of it observed in the logs:

1. Groq `openai/gpt-oss-20b` 429s on TPM.
2. Cascade falls to `gemini-2.5-flash-lite` — **20% of all routes**. This is the
   rung [[control-command-misroute-by-weak-rung]] already documents as
   hallucinating actions on terse commands. All 15 misrouted play requests came
   from it: "Play Duck Gangsta" → `apply_effect: darth_vader`; "play
   radioactive" → `skip`; a YouTube URL → `apply_effect: slowed`.
3. Gemini 429s too (quota exhausted).
4. Remaining rungs are **dead models** (below).
5. Nothing answers → `"brain glitched, say that again"`.

### Why effects "don't apply / take a while"

Same cause, different surface. Consecutive effect requests get routed by
*different models* depending on who is rate-limited at that instant, and the
models disagree on `mode` semantics. Observed 04:53–04:57 on 2026-09-06: the
same "overload" request routed three times as `add`, then no mode, then
gemini's `replace` — and `replace` **silently wiped** the `darth_vader` the user
had just added. Then "can you apply a super slowing effect" routed to
`reverb/more`. Compounding it, every effect reply is `[SILENT]`, so the user
gets no feedback that anything happened and asks again — which spends more TPM,
which makes the next route worse.

### Five dead model ids, verified against live catalogs

The freshness gap [[voice-llm-model-deprecated-and-never-wired]] explicitly
predicted (*"groq_model … is exactly as exposed: a single hardcoded default
with no freshness check. Same risk, not yet realized"*) is now realized five
times over:

| Config field | Model | Status (live catalog check) |
|---|---|---|
| `groq_vision_model` | `meta-llama/llama-4-scout-17b-16e-instruct` | absent from Groq |
| `nvidia_model`, `agent_nvidia_model` | `qwen/qwen3-next-80b-a3b-instruct` | absent from NVIDIA (all qwen removed; 81 models, zero qwen) |
| `agent_google_model_fast` | `gemini-3-flash` | absent from Gemini |
| `browser_google_model` | `gemini-2.0-flash` | absent from Gemini |
| Cerebras (all) | — | `cerebras` SDK not installed in the image |

Also stale rather than dead: `gemini_router_model` is `gemini-2.5-flash-lite`
while `gemini-3.5-flash`, `3.5-flash-lite`, `3.6-flash`, `3.7-flash` and
`3.8-flash` are all live on our key. And `gemini_router_model_alt` is `""`, so
the overflow rung that exists specifically so "the alt isn't 429'd when the
primary is" is inactive.

### A regression this project introduced

PR #9 pointed `voice_llm_model` at `openai/gpt-oss-20b` — **the same model as
`groq_model`**. Groq rate-limits per model, so before that change voice replies
drew on `llama-3.1-8b-instant`'s separate bucket. Afterward, routing (~3,100
tok) and voice replies (~1,000 tok) drained one 8k bucket, cutting effective
capacity from ~2.6 to **~1.95 requests/minute**. Correct fix for a dead model,
wrong second-order effect, not caught because nothing tested for it.

## Why four previous fixes did not fix it

[[groq-daily-cap-routing-storm]] (Jun 8), [[cascade-outage-nvidia-hang-gemini-rpm]]
(Jun 9), [[slim-routing-prompt]] (Jun 10, −618 tok) and [[slim-tool-schemas]]
(Jun 15, −681 tok) each attacked the *magnitude* of the per-call cost. None
changed the *structure*: every utterance still pays full routing overhead
against a per-minute budget, so the system degrades hardest exactly when it is
being used most. [[slim-tool-schemas]] projected "~5 routes/min under 8k TPM";
measured reality today is ~2.6, because the 2,531-token fixed floor (prompt +
schemas) survives every trim.

Both documented next steps were deferred — and **one of them is now
contraindicated by evidence**: [[slim-tool-schemas]] listed "flip to
Gemini-primary" as the second lever, but today's data shows Gemini-flash-lite
is the source of all 15 misroutes. Promoting it would trade 429s for wrong
answers.

## Fix

Shipped so far (this incident stays `active` until the rest lands):

- **`voice_llm_model` → `qwen/qwen3.8-27b`** — a *different* model from
  `groq_model`, restoring separate TPM buckets, and independently justified by
  measured latency + quality ([[voice-model-eval-2026-09-06]]).
- **Regression guard** `test_voice_llm_model_defaults_to_a_different_model_than_groq_model`
  so the two roles can never silently re-share a bucket.

Still outstanding:

- Replace the five dead model ids with ones verified present.
- Upgrade `gemini_router_model` off 2.5-flash-lite, and populate
  `gemini_router_model_alt`. **Must be eval-gated** — the misroute history is
  exactly why a newer Gemini cannot simply be assumed better.
- Install or formally drop the Cerebras rung (it currently fails at every
  startup and contributes nothing but latency and log noise).
- **Automated model-freshness check** validating every configured model id
  against its provider's live catalog. Asked for twice in the vault; never
  built. It is the only thing that would have caught any of these five before
  users did.

## Validation

- Failure-class census scripted over all 129,196 log records, not sampled.
- Token costs measured inside the running container, not estimated.
- Every "dead model" claim verified against the provider's own live catalog
  API; error codes (404/410) were treated as a hint, not proof.
- Deliberately NOT proposed: the deterministic control-verb short-circuit —
  the operator declined that in [[groq-daily-token-cap-degrades-routing]]
  ("don't make `_music_safety_net` do more") as conflicting with the
  agentic-mastery north-star.
