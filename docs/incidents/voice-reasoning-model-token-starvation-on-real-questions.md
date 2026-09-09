---
type: incident
status: resolved
date: 2026-09-09
tags: [voice, brain, groq, reasoning-model, token-budget, qwen]
related: [[voice-llm-model-deprecated-and-never-wired]] [[voice-model-eval-2026-09-06]]
---

# `qwen3.8-27b` burned its whole voice-reply token budget on hidden reasoning for a real question — silent "got nothing to say"

## Symptom

Operator, live in VC: asked Poob a genuine Rainbow Six Siege strategy
question twice, 15 seconds apart, and got `"got nothing to say right
now"` both times.

```
01:33:02  "Hey, Poob. Which attacker should I start with? Obviously,
           we're playing siege, so give me a real answer."
          -> First sentence ready: "got nothing to say right now" (1592ms)

01:33:17  "Oh my god. Hey, Poob. Answer the fucking question..."
          -> First sentence ready: "got nothing to say right now" (1938ms)
```

## Root cause

The casual voice-reply path (`respond_streaming`, the `any_yielded` check
at poob.py) yields this exact fallback line when the LLM stream completes
successfully but produces zero non-empty sentences. Reproduced live,
deterministically, 8/8:

| max_tokens | reasoning_tokens used | content produced |
|---|---|---|
| 200 (the actual production value at the time) | 200/200 | **empty** |
| 300 | 300/300 | **empty** |
| 500 | 182-225 | some (borderline — stochastic, not a safe margin) |
| 1000 | 260 | reliable |

`voice_llm_model` (`qwen/qwen3.8-27b` since PR #12) is a reasoning model:
it spends part of every token budget on hidden chain-of-thought before any
visible text, exactly the same class of bug already fixed once for
`gpt-oss-20b` ([[voice-llm-model-deprecated-and-never-wired]]). What's
different this time: **the reasoning cost is content-dependent, not a
small fixed overhead.** A real question requiring an actual answer ("which
attacker") costs 200-300 reasoning tokens; casual banter costs far less.

**Why the PR #12 eval didn't catch this**: every eval prompt was
roast/banter material — "someone just played nickelback", "what do you
think of my haircut" — deliberately low-cognitive-load by design (that's
literally the persona's job). None of them required the model to actually
*think* about a real answer, so the eval never exercised the failure mode
a genuine question triggers. 0/18 empty in that eval was a true, honestly-
measured result — for the population of prompts tested. It was silently
not representative of "a person asks Poob something real."

**Corrected production value**: `voice_llm_max_tokens` (config) is what
actually gets wired into `PoobBrain.max_tokens_voice` via main.py — the
production value at the time of the incident was **200**, not the
`PoobBrain` class's own bare default of 110. Both numbers were wrong; both
are fixed.

## Fix

Per operator instruction: raise generously, not to the bare minimum that
happened to work once. `max_tokens_voice` / `voice_llm_max_tokens` raised
**110/200 → 1000** — well past the measured worst case (260 reasoning
tokens), with real headroom for a "let it breathe" 3-4 sentence answer if
the model genuinely wants to give one. This is a ceiling, not a target:
the persona prompt's own "default tight, breathe when it earns it"
instruction is what keeps normal replies short, unchanged.

The three `toob_max_tokens = min(max_tokens, 100)` sites (personality-wrap
call sites — same `voice_llm_model`, same reasoning-model starvation risk,
just with a shorter target output) share the identical mechanism and were
raised the same way: **100 → 400**.

## Validation

- `tests/unit/test_voice_token_budget_floor.py` (3, new): `PoobBrain.max_tokens_voice`
  and `AppConfig.voice_llm_max_tokens` both stay above the measured
  full-starvation point (300); the two defaults can't silently drift apart
  from each other again (they did once already: 110 vs 200).
- `tests/unit/test_music_text_wrap.py::test_music_wrap_caps_tokens_tightly`
  and `tests/unit/test_voice_hallucination_drop_speaks.py::test_no_command_reaction_uses_same_tight_token_budget_as_toob_wrap`
  gained a **floor** assertion (`> 300`) alongside their existing ceiling
  assertion (`<= 400`). Both only ever checked "not too high" before —
  exactly the coverage gap that let a too-low ceiling ship silently. Now
  checks both directions.
- Mutation-verified: reverted `max_tokens_voice`/`voice_llm_max_tokens` to
  110/200 and the three `toob_max_tokens` ceilings to 100, confirmed every
  new/updated assertion failed with a message pointing at this incident,
  restored, confirmed all pass.
- Full unit suite: 2018 passed, 1 skipped, no regressions.
- Root cause confirmed via live API calls against the real production
  system prompt and the actual conversation-context wrapper shape (not a
  synthetic short prompt) — the first isolated test with a bare short
  question passed 0/10 empty; only reproducing the REAL message shape
  (passive conversation context + "=== speaking to you RIGHT NOW ===" +
  a real question) reproduced the failure deterministically.

## Follow-ups

- This is the second time a reasoning-model token-budget miscalibration
  has shipped for `voice_llm_model` (`gpt-oss-20b` 2026-08-26,
  `qwen3.8-27b` 2026-09-09). A model-agnostic guard — measuring reasoning-
  token spend against a REAL question sample, not just a casual-chat
  sample, before any future `voice_llm_model` swap — would catch this
  class of bug before it ships rather than after. Worth writing into
  [[voice-model-eval-2026-09-06]]'s methodology as a required test
  category, not an optional one.
