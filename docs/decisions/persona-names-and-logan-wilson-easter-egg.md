---
type: decision
status: active
date: 2026-09-10
tags: [brain, persona, prompt]
related: []
---

# Persona prompt: use names more, and a rare Logan Wilson easter egg

## Decision

Two additions to `_build_system_prompt`'s RULES block (`src/poob/brain/poob.py`),
operator-directed, not bug fixes:

1. **Use people's actual names more** — especially when directly addressing
   someone or reacting to something they specifically said, instead of
   defaulting to "you"/"dude". Explicitly capped ("one natural mention is
   plenty, not every line") so it doesn't fight the existing "avoid
   vocatives" rule right above it, which is about not *leading* a response
   with someone's name — using a name mid-response is a different thing
   and both rules coexist fine.
2. **A rare Logan Wilson reference** — roughly 1 response in 10, any angle
   (blame him, compare someone to him, bring him up at random), never
   explained, never forced. This is intentionally an LLM-prompted
   probability, not a code-level dice roll like `BOOB_PROBABILITY` — the
   operator asked for it in the prompt specifically, and the exact rate is
   flavor, not something that needs code-level enforcement precision.

## Why this needs a note

If a future agent reads this prompt cold, an unexplained instruction to
randomly bring up a specific named individual will look exactly like the
class of thing CLAUDE.md says to treat as suspicious residue and remove.
It isn't — it's a deliberate, operator-requested running bit. Don't strip
it without asking first.

## Validation

- `tests/unit/test_brain_casual_fallback.py`: `test_persona_prompt_encourages_using_names`,
  `test_persona_prompt_has_logan_wilson_easter_egg` — pin both additions'
  presence in the casual-generation prompt. Mutation-verified: removed the
  block, confirmed both tests failed, restored, confirmed pass.
- Full unit suite: 2047 passed, 1 skipped, no regressions.
- Not yet verified against a live model response (prompt-only change,
  no code path exercises the actual generation here) — the operator can
  confirm the vibe lands correctly once deployed.
