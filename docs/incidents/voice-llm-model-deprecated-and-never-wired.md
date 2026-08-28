---
type: incident
status: resolved
date: 2026-08-26
tags: [brain, voice, groq, llm, config, dead-code, reasoning-model]
related: [[groq-gpt-oss-20b-swap]] [[free-llm-tier-audit-2026-06]] [[drop-cerebras-from-cascade]] [[voice-hallucination-drop-gets-a-line]]
---

# `llama-3.1-8b-instant` was removed from Groq's catalog — and the config field meant to swap it had never been wired up

## Symptom

Live production report: *"it will take here and there but most importantly
won't play music."* Confirmed in `poob.jsonl`:

```
02:59:06  Speculative wrap failed  error=404 'The model `llama-3.1-8b-instant` does n...'
01:35:02  Voice streaming failed   error=404 (same model)
          -> "First sentence ready: brain glitched, say that again"
01:35:36  Voice music route (streaming) failed  error=404 (same model)
          -> "First sentence ready: something went wrong with the music"
```

Most `play X` requests still succeeded — the primary tool-routing call uses a
*different* model (`groq_model`, `openai/gpt-oss-20b`) and was unaffected.
The break was specifically in the fast quick-reaction text layer: personality
wraps, casual chat, and — critically — the response given when a request is
genuinely ambiguous ("play the music" with no content). That last one is why
it read as "won't play music": the correct behavior (ask "Play what?") was
replaced by a hardcoded failure line.

## Root cause — two independent bugs, both hit at once

1. **The model is gone.** Verified live against Groq's own `models.list()`
   (via the app's real API key, not assumed from a changelog):
   `llama-3.1-8b-instant` does not appear in the current catalog at all. This
   is Groq deprecating and removing the model, not a rate limit or transient
   outage — every single call 404s, permanently.

2. **The config field meant to fix this had never been wired up.**
   `AppConfig.voice_llm_model` existed with exactly this purpose ("Fast Groq
   model for voice") — but `main.py`'s `PoobBrain(...)` construction never
   passed it. All 6 call sites in `brain/poob.py` hardcoded the literal
   string `"llama-3.1-8b-instant"` directly, disconnected from config
   entirely. Changing the env var would have done nothing. This is the same
   class of bug as [[no-ci-test-gate]]'s per-subsystem-logging example: a
   feature that exists in the code but was never actually connected.

## Fix

- **New `voice_llm_model` field on `PoobBrain`**, defaulting to
  `openai/gpt-oss-20b` — the single source of truth, referenced by all 6
  call sites (`model=self.voice_llm_model`) instead of six independent
  literals.
- **Wired through `main.py`**: `voice_llm_model=config.voice_llm_model` now
  actually passed into the constructor. A future model swap is a one-line
  env var change, not a six-site hunt through `poob.py`.
- **Model choice is evidence-based, not a guess.** Verified live before
  picking `gpt-oss-20b`:
  - It's already proven in this exact production environment as the primary
    tool-routing model (`groq_model`), with sub-second latency.
  - [[free-llm-tier-audit-2026-06]] (the research this repo already had)
    benchmarked it at **~485ms vs the dead model's ~720ms** on this exact
    workload, and found no clearly-better free alternative — Gemini
    included — for this specific quick-reaction voice role.

## The second bug this fix nearly shipped

`gpt-oss-20b` is a **reasoning model**: it spends part of `max_tokens` on
hidden chain-of-thought before any visible answer, and that spend is
**stochastic per call** — measured 6 to 78 reasoning tokens on an identical
prompt set against the live API. A naive model swap (same token budgets,
no other change) would have "fixed" the 404 and silently reintroduced empty
responses under a different failure signature:

| Config tested | Empty-response rate |
|---|---|
| gpt-oss-20b, 40 tokens, no `reasoning_effort` | ~100% (reasoning ate the whole budget) |
| gpt-oss-20b, 200 tokens, no `reasoning_effort` | still failed once directly observed |
| gpt-oss-20b, 40 tokens, `reasoning_effort="low"` | ~20% (1/5 in one trial) |
| gpt-oss-20b, 60-200 tokens, `reasoning_effort="low"` | **0/40** across live trials |

`qwen/qwen3.6-27b` (the only other general-purpose chat model currently free
on Groq) was tested and **rejected** — it emits its chain-of-thought as
literal visible text (`<think>...`) inside the same `content` field, so at a
constrained token budget it never reaches a real answer at all: 0/5 trials
produced anything but raw "thinking process" text, which would have been
spoken to the user verbatim. Worse than the reasoning-model-with-hidden-
channel failure mode, not better.

Groq's `reasoning_effort` accepts only `low`/`medium`/`high` — unlike
Gemini's `reasoning_effort: "none"` ([[free-llm-tier-audit-2026-06]]), there
is no way to fully disable reasoning for gpt-oss models on Groq.

**Fix, in addition to the model swap:**
- `reasoning_effort="low"` added to all 6 call sites.
- The three `toob_max_tokens = min(max_tokens, 40)` caps raised to `100` —
  40 was calibrated for the old non-reasoning model, where 40 tokens was the
  *entire* usable budget; for a reasoning model, an equivalent visible-content
  budget must leave headroom above the stochastic reasoning spend.
- The `boob_max_tokens` site (already 200) was **already broken** before
  this fix shipped — confirmed empty at 200 tokens with no
  `reasoning_effort` set, independent of the 404. `reasoning_effort="low"`
  fixes it; the 200 ceiling is unchanged.

**No token budget makes this mathematically airtight** — reasoning length is
stochastic, so some non-zero failure probability remains at any finite
budget. 0/40 in live trials is strong evidence, not a proof. If empty
responses recur, the next lever is either a still-higher budget or an
application-level empty-content retry; not attempted here because the
measured rate at the shipped configuration is indistinguishable from zero.

## Third bug the raised token budget surfaced

Raising `toob_max_tokens` from 40 to 100 broke two existing tests that
hardcoded the old ceiling as a magic number:
`test_music_wrap_caps_tokens_tightly` and
`test_no_command_reaction_uses_same_tight_token_budget_as_toob_wrap`. Both
were legitimate, non-vacuous guards against unbounded ballooning — not
vacuous tests in the sense found earlier this project — they simply pinned
the specific old value. Updated to `<= 100` with the rationale recorded in
each docstring, so a future reader doesn't have to reconstruct why the
number changed. Full suite re-confirmed green after the update.

## Validation

- Model absence confirmed by calling Groq's live `models.list()` from inside
  the running container with the real API key — not inferred from docs.
- Replacement latency claim sourced from existing repo research
  ([[free-llm-tier-audit-2026-06]]), not re-benchmarked from scratch.
- Empty-response rate measured directly against the live API at every
  candidate token budget (40/60/80/100/200) before choosing 100/200.
- `qwen3.6-27b` alternative tested and rejected on measured evidence, not
  assumption.
- Two regression tests, both mutation-verified (removed the fix, confirmed
  the test fails; restored, confirmed it passes again):
  - `test_main_wires_config_voice_llm_model_into_poobbrain` — structural
    guard on the wiring itself (a behavioral test can't see a missing
    keyword argument in an 800-line startup function).
  - `test_casual_text_fallback_uses_configured_voice_model_with_low_reasoning`
    — asserts the real API call carries both `model` and `reasoning_effort`.
- Full unit suite green.

## Addendum (2026-08-28) — the fix itself shipped a corrupted line

Two days after this fix deployed, every casual voice response started
failing again: `"AsyncCompletions.create() got an unexpected keyword
argument 'temperature_PLACEHOLDER_removed'"` — the exact "brain glitched"
failure class this incident exists to close.

Root cause: mutation-testing residue. While verifying the
`reasoning_effort` regression guard during this fix, a `sed` command
temporarily corrupted one call site into
`temperature_PLACEHOLDER_removed=0,` for the mutation test. The restore
step searched for and reinserted a *missing* `reasoning_effort` line via a
different code path, and never noticed the corrupted line still sitting
next to it. Both landed in the diff; the full 1964-test suite passed
anyway, because every test mocked the Groq client with `AsyncMock()` or a
hand-built fake — both accept any keyword argument silently. The real SDK
has no such tolerance; it raised immediately in production.

Fixed in a follow-up PR with a structural test that parses `poob.py`'s AST
and validates every `client.chat.completions.create(...)` call against the
real `AsyncCompletions.create` signature via `inspect.signature` — the
exact check no mock could provide. Mutation-verified against the precise
corruption that shipped.

**The lesson, stated plainly:** a mock that accepts any keyword argument
cannot tell a correct call from a typo. Any test suite built entirely on
such mocks has this blind spot everywhere Groq (or any SDK with a strict
signature) is called — this incident closed it for the 6 sites this fix
touched; the same gap may exist elsewhere in the codebase.

## Follow-ups

- No alert exists for "a model referenced in code was removed from the
  provider's catalog." This was found live, mid-outage, by reading logs —
  not by any automated signal. Same shape as [[concurrent-builds-race-latest-tag]]
  and the per-subsystem-logging gap: the pipeline was green, the outage was
  real, and nothing connected the two. Worth a periodic live model-list
  check against every hardcoded/configured model id, at least for the
  hot-path providers (Groq, Gemini).
- `groq_model` (the tool-routing model) was not affected by this incident,
  but it is exactly as exposed: a single hardcoded default with no
  freshness check. Same risk, not yet realized.
