---
type: decision
status: active
date: 2026-07-09
tags: [voice, brain, music, tool-calling, toob]
supersedes: []
related: [[tool-hallucination-from-passive-context]] [[wake-word-dual-gate]]
---

# Voice hallucination-drop now speaks a line instead of staying silent

## Context

[[tool-hallucination-from-passive-context]] documents, as deliberate and
tested, that when `_handle_music_voice_streaming`'s hallucination guard drops
a fabricated `play` query (stale-context pull, no genuine play-intent in the
current turn), **voice returns empty string — no TTS** — while text gets a
brief acknowledgement. The stated reasoning: voice channels have constant
crosstalk and multiple simultaneous speakers, so speaking up every time an
ambiguous utterance technically passes the wake gate risks Poob constantly
interrupting live conversation with "I didn't understand."

Operator report (2026-07-09): a user said "Hey, Poob" at the end of an
unrelated sentence ("That's it? That's the song ends right there. Oh, wow.
Hey, Poob."), the dual wake-gate correctly fired (`addressed=True` — this
wasn't a marginal/ambient false-positive, the user genuinely said the wake
phrase), the router hallucinated `action=play, query='homework drops as
donut'` from stale context, the guard correctly caught and dropped it — and
Poob said nothing. From the user's side this reads as "the bot ignored me,"
which is a materially different experience than "the bot correctly declined
a nonsense request." Escalated as "this cannot keep happening" / "fix the
issue, it didn't respond when addressed."

## Decision

**When the drop happens on a call that already passed the dual wake-gate
(i.e., `_handle_music_voice_streaming` was reached at all — which requires
`addressed=True` upstream), speak a short, in-character line instead of
staying silent.** New method `_stream_toob_no_command_understood` — same
model/token-budget/streaming pattern as the existing `_stream_toob_wrap_from_query`,
but reacts to "was addressed with no real request" rather than "reacts to a
real request." Wired into the hallucination-drop branch of
`_handle_music_voice_streaming` in place of the bare `return`.

This does **not** reopen the false-positive-wake concern the original design
protects against: the wake-gate dual-check ([[wake-word-dual-gate]]) already
runs *before* `respond_streaming`/`_handle_music_voice_streaming` is ever
invoked in voice. Every call into this function has already cleared
`addressed=True` — text_match confirmed, and either audio_match too or the
bot was silent (no mic-loopback risk). So this change never fires from
ambient background noise; it only fires when the user genuinely said the
wake phrase but the *routed action* turned out to be a fabricated one. The
"don't interrupt every ambiguous utterance" protection stays fully intact —
this is strictly about what happens *after* a confirmed address, not a
loosening of the wake gate itself.

## Alternatives considered

- **Leave it silent, explain the tradeoff to the operator.** Rejected —
  operator explicitly decided the tradeoff (occasional short in-character
  line vs. dead air on a confirmed address) in favor of always responding.
- **Fall through to a full casual-chat reply** (reusing `_casual_text_fallback`).
  Rejected as heavier than needed: that path is tuned for open-ended
  conversation, not a tight one-line reaction, and would require breaking
  the persona-already-committed context (`VOICE_TOOB`/`VOICE_BOOB` is yielded
  by the caller before this function runs). A short Toob line matches the
  persona already signaled to the TTS pipeline.
- **Move the hallucination check earlier, before the VOICE_TOOB/BOOB persona
  signal, so a drop never commits to a persona at all.** Bigger refactor
  (would require restructuring `respond_streaming`'s dispatch order); not
  needed since Toob reacting to "you said my name for nothing" is in-character
  either way.

## Consequences

- Every confirmed-address-but-hallucinated-drop now costs one short Groq call
  (`llama-3.1-8b-instant`, same tiny budget as the existing Toob wraps) —
  negligible cost, same provider/model already used for this exact class of
  reaction.
- If this reaction is ever heard MORE than very occasionally, that's a signal
  the upstream hallucination rate itself needs attention (see
  [[tool-hallucination-from-passive-context]]) — this change makes the
  hallucination audible/attributable instead of silently swallowed, which is
  a net observability improvement, not just a UX patch.
- Does not touch the TEXT-mode drop path (`_handle_music`'s equivalent block) —
  text already gets "I didn't catch a music request there.", unaffected.
- Does not touch `_music_safety_net`'s own silence behavior for genuinely
  unaddressed/no-tool-at-all cases — this is scoped exactly to the
  hallucination-drop-after-confirmed-address branch.
