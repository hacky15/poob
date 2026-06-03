---
type: incident
status: resolved
date: 2026-06-03
tags: [brain, music, routing, llm, false-positive, prompt]
related: [[music-routing-prompt-thoughtfulness]] [[gemini-tool-router-rung]] [[tool-hallucination-from-passive-context]]
---

# "Flip a coin" played a coin-flip sound clip instead of answering

## Symptom

Operator: *"I asked 'poob flip a coin' and it literally played a YouTube clip. That cannot be how it works — it should have simply done a text response."*

Prod log (2026-06-03 00:52):

```
text='Hey, Poob. Flip a coin.'  wake_word=True
poob.tool_route  args={'action':'play','query':'coin flip sound'}  provider=gemini  tool=music_assistant
music.action     action=play query='coin flip sound'
music.response   'Queued Coin Flip | Free Sound Effect [0:04] at position 1.'
Playing track    title='Coin Flip | Free Sound Effect'
```

## Root cause

Not a weak-fallback over-route — **Gemini** (the reliable primary-fallback rung, [[gemini-tool-router-rung]]) made a creative-but-wrong call: it interpreted "flip a coin" as "play a coin-flip *sound effect*" and routed to `music_assistant(play, query='coin flip sound')`. The query passed the hallucination guard (its tokens overlap the message), because this isn't a stale-context hallucination — it's an **intent misclassification**: the model used music to *act out* a request rather than recognizing music_assistant is for audio the user wants to *listen to*.

The wording invited it: `MUSIC_TOOL.description` opened with *"Handle ANY music or audio playback request"* — "any audio playback" reads as "play a sound for whatever they asked."

## Fix

Prompt-layer (the operator's call: foundational, not a hardcoded denylist), at both surfaces the model sees:

1. **`MUSIC_TOOL.description`**: opening changed from "Handle ANY music or audio playback request" to *"Play or control MUSIC the user wants to HEAR — songs, artists, genres, playlists. NOT for sound effects standing in for a non-music request (e.g. do NOT play a 'coin flip sound' for 'flip a coin' — that's answered in text)."*
2. **System prompt music block** (`_build_system_prompt`): added the principle *"music_assistant is for music the user wants to HEAR … not for acting out a request with a sound effect, and not everything with the word 'play' is music"*, plus a NOT-music example group answered in text: *"'flip a coin' / 'roll the dice' / 'pick a number' (just do it and say the result — NEVER play a 'coin flip sound' clip to fake it)."*

Crucially **balanced** per operator guidance ("be DILIGENT in finding music request queries"): the positive catch-real-requests directive + nonsense-name examples (tiki tiki / cheeky cheeky) are kept and reinforced ("Be DILIGENT about catching real song requests"), so tightening false positives doesn't reintroduce the earlier false-negative problem ([[music-routing-prompt-thoughtfulness]]). "Flip a coin" now falls through to the casual path → Poob answers in text (heads/tails, in character).

## Validation

- `tests/unit/test_brain_casual_fallback.py`: `test_music_prompt_rejects_sound_effect_fulfillment` (flip-a-coin + "hear" + "sound effect" present in prompt AND tool desc); `test_music_prompt_stays_diligent_on_real_requests` (the "diligent" directive + tiki/cheeky positives survive).
- Full unit suite green.
- Post-deploy: "flip a coin" / "roll the dice" should get a text reply, no `music.action`; real "play X" still routes (watch `poob.tool_route tool=music_assistant` stays healthy for genuine requests).

## Follow-up

If we later want a *true* random coin flip / dice (LLMs aren't reliably random), a tiny deterministic "fun" handler is the clean route — deferred; the operator asked only for a text response, which the casual path now gives.
