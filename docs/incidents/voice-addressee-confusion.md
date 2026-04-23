---
type: incident
status: resolved
date: 2026-04-22
tags: [voice, brain, llm-prompting]
related: [[poobbrain-architecture]] [[voice-architecture]]
---

# Poob addresses the wrong user by name in voice replies

## Symptom

Ben asked "How long was the song that you just played?" — Poob replied **"lab rat, are you asking because..."**. Jeweinery then addressed Poob — Poob replied **"lab rat, you're speaking to me..."**. Neither user is Lab Rat. Lab Rat had been dominating the VC with long passive utterances.

## Root cause

The prompt assembled in `session.py:_process_single_response` concatenated:

1. 15 lines of passive transcript (all users' speech, formatted `"Lab Rat (labrat24): blah"`).
2. The current speaker's address, buried at the bottom: `"Ben (hacky15) said to you: <transcript>"`.

Smaller models (Groq llama-3.3-70b, Cerebras qwen-3-235b) weight the most-frequent name in context when picking a vocative. Lab Rat was over-represented. Compounding this, the Poob system prompt told the model *"Use people's names when talking to them (from the transcript)"* — actively encouraging the wrong behavior.

## Fix

Two changes, both at the right layer:

**1. `src/poob/voice/session.py` — make the current speaker visually unmissable.**

```python
prompt = (
    f"[Recent conversation you've been listening to:\n{conv_context}]\n\n"
    f"=== The user speaking to you RIGHT NOW is {user_name} ===\n"
    f"{user_name} just said to you: {transcript}\n"
    f"(If you address them by name at all, use ONLY \"{first_name}\" "
    f"— never another user's name from the transcript above.)"
)
```

Three independent signals pointing at the same identity: `=== ===` delimiter, explicit "RIGHT NOW", and a negative instruction.

**2. `src/poob/brain/poob.py` — stop encouraging vocatives in voice.**

Replaced "Use people's names when talking to them (from the transcript)" with "In voice, avoid vocatives; if you absolutely must address someone, use ONLY the name marked as the CURRENT SPEAKER — never a name from the passive transcript."

## Why not strip passive context

Passive context stays in the prompt. "Skip the song", "who said that thing about X", and similar requests need it. The fix is at the identity layer, not the context layer.

## Validation

- 902/902 unit tests pass.
- Deploy signal: subsequent `Response complete` lines should not open with a vocative belonging to a different user.
