---
type: research
status: active
date: 2026-09-06
tags: [brain, llm, voice, groq, eval, benchmark, persona]
related: [[routing-tpm-ceiling-degrades-cascade]] [[voice-llm-model-deprecated-and-never-wired]] [[free-llm-tier-audit-2026-06]]
---

# Voice-model eval: gpt-oss-20b vs qwen3.8-27b for the quick-reaction voice slot

## Why

Operator standing rule, stated 2026-09-06: *"if you ever make an llm brain
change, you must VERIFY with EVALUATIONS that its faster and more intelligent
than the previous."* This note is that verification for the
`voice_llm_model` swap. It also answers a second operator question — whether
the personality was failing because of the prompt or the model.

## Method

Identical production persona prompt (`_build_system_prompt(7, voice=True,
with_tools=False)`, 1,140 tok) sent to each candidate via the real Groq SDK
from inside the prod container.

- **Quality/reliability run:** 6 utterances × 3 reps = 18 samples per model.
  Measured: empty-response rate, visible `<think>` leak rate, profanity rate
  (the operator's actual target register), plus captured samples.
- **Latency run, separate and deliberately clean:** `Groq(max_retries=0)` so a
  429 surfaces as an error instead of silent SDK backoff, models **interleaved**
  to control for drift, 9s spacing to keep TPM pressure out of the numbers,
  n=6 each.

The two runs are separate on purpose — see the correction below.

## A measurement error worth recording

The first latency figure taken was a **single sample per model** (307ms vs
595ms) and suggested qwen was ~2× faster. That was wrong. A subsequent 36-call
burst then reported medians of 9,021ms and 5,038ms — also wrong, in the other
direction, because rapid sequential calls hit TPM and the SDK's internal retry
backoff was being counted as model latency.

Neither number was trustworthy. Only the third run — retries disabled,
interleaved, spaced — measures the model. **A latency benchmark that shares a
rate-limited bucket with itself is measuring the rate limiter.**

## Results

| | `openai/gpt-oss-20b` (was) | `qwen/qwen3.8-27b` (now) |
|---|---|---|
| Median latency | 601 ms | **438 ms** |
| Min / max | 400 / 752 ms | 355 / 576 ms |
| Empty responses | 0 / 18 | 0 / 18 |
| Visible `<think>` leak | 0 / 18 | 0 / 18 |
| Profanity present | 6 / 18 (33%) | **9 / 18 (50%)** |
| Accepts `reasoning_effort` | yes | yes (verified — call sites unchanged) |

**~27% faster**, not the 2× the bad first sample implied.

### Quality, on the operator's actual target

The ask was "out of pocket, not corny-roasty, takes things way too far." Same
prompt, same input ("someone just played nickelback"):

- `gpt-oss-20b` → *"that band is like a stale pizza slice that just keeps
  getting reheated"* — the simile-roast register the operator explicitly
  rejected as corny.
- `qwen3.8-27b` → *"I can literally taste the mediocrity in my throat. It's
  like licking a gas station tire. How does your hearing work"* — specific,
  disproportionate, no wind-down, no wink.

The finding that mattered: **the vulgarity ceiling was the model, not the
prompt.** `gpt-oss-20b` is safety-tuned and reverts to sanitized simile humor
regardless of instruction; a first small sample showed zero profanity from it,
though at n=18 it does reach 33% — so it is *less consistent*, not incapable.
No prompt rewrite closes that gap; a model change does.

## Rejected candidates

- **`qwen/qwen3.6-27b`** — emits its chain-of-thought as visible text
  (`<think>` / "Here's a thinking process:") inside `content`, which would be
  spoken aloud verbatim. Independently re-confirmed here; matches the same
  rejection recorded in [[voice-llm-model-deprecated-and-never-wired]]. That
  the method reproduced a known prior result is itself a check on the method.
- **`openai/gpt-oss-120b`** — does swear freely and reads well, but it is
  slower (759ms single sample) and is already `agent_groq_model`, so choosing
  it would recreate the exact per-model TPM contention this swap exists to
  break.

## The decisive reason, which is not quality at all

`qwen3.8-27b` is a **different model from `groq_model`**. Groq rate-limits per
model, so this change alone restores the separate TPM bucket that PR #9
accidentally collapsed — worth more in practice than either the latency or the
register win. See [[routing-tpm-ceiling-degrades-cascade]].

## Limits of this eval

- n=6 for latency and n=18 for quality are enough to choose between two
  candidates; they are not enough to rank near-ties, and profanity rate is a
  crude proxy for "funny."
- Only Groq-hosted candidates were tested. Gemini 3.5–3.8 flash models are live
  on our key and untested for this slot; they sit behind a different quota
  system and would need their own run.
- Nothing here evaluates the **routing** model, which is a different job
  (tool-call accuracy, not register). `tests/manual/bench_router_providers.py`
  with its MATRIX oracle is the right harness for that; it was not run here.
