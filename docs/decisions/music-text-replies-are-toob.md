---
type: decision
status: active
date: 2026-07-12
tags: [music, brain, toob, persona, voice, text]
related: [[boob-music-wrap-variant]] [[speculative-music-wrap]] [[music-text-wrap-inherited-deal-instructions]] [[now-playing-card-snapshot-after-brain-call]]
---

# Music replies are Toob's everywhere — text channel AND the VC speak of text requests

## Context

Prod, 2026-07-12: "@Poob play backwoods 808 fishing" (text @mention) got
*"Now that be some sick ass lake vibes, trust me."* — a chummy normal-Poob
line — and the VC heard it in **Fenrir (Poob's voice)**. Operator: *"it's
supposed to just be toob. toob can say it in chat but most importantly he
says it in voice."*

The old convention (recorded in `_wrap_music_response_text`'s docstring and
pinned by test: *"the persona swap only matters for TTS pitch/timbre, so
text stays Poob"*) treated Toob as voice-only. The operator has reversed
that: **the music-wrap persona is Toob in every modality.**

## Decision

1. **One wrap.** `_handle_music`'s text branch now calls
   `_wrap_music_response` — the SAME Toob wrap the voice path uses (dark,
   one-sentence, venomous). The dedicated Poob text wrap
   (`_wrap_music_response_text`) is **deleted**. The anti-deal-vocabulary
   contract from [[music-text-wrap-inherited-deal-instructions]] carries
   forward — the shared wrap's prompt is music-only, and the tests guarding
   deal-vocabulary leakage now run against it.
2. **Sentinel-tagged transport.** Text-mode music replies return
   `VOICE_TOOB + wrapped` — the single-string analog of the sentinel
   `respond_streaming` yields as its first item. `AgentMessageHandler`
   (`_split_persona`) strips it before posting (the sentinel must NEVER
   appear in a Discord message) and passes `persona="toob"` onward.
   `[SPEAK]` verbatim answers are tagged in text mode only — embedding the
   sentinel in voice mode would leak it into TTS (voice callers yield it
   separately). History saves the CLEAN text.
3. **Toob speaks text requests in VC.** `speak_if_in_channel` takes a
   `persona` arg and dispatches through the same string-keyed synth table
   the session uses (`poob/toob/boob`) — a text music request from someone
   in the VC is now spoken through `_synthesize_toob` (Enceladus + filter
   chain), not Fenrir.

## Boundaries (deliberate)

- **`[SILENT]` control acks stay persona-neutral** ("Skipped X.",
  "Autoplay enabled."). In voice they are never spoken; in text they post
  as plain status and speak (if at all) as Poob. Controls have never been
  persona bits — matches [[boob-music-wrap-variant]]'s "skip/pause/stop/
  volume never trigger either persona."
- **No Boob roll on text.** Boob (~1/75) remains a voice-streaming-only
  bit; text is always Toob. The `_split_persona` helper already understands
  the `VOICE_BOOB` sentinel, so extending the roll to text later is a
  one-line change in `_handle_music`.
- **Long/raw informational output** (queue listings, lyrics) stays
  unwrapped and unmarked — it's data, not a persona line.
- **No "Toob:" name prefix in the posted text.** Toob never introduces
  himself ("your voice IS your identity"); in text his venom is the
  identity. Revisit only if users report confusion.

## Alternatives considered

- **Out-of-band persona channel** (instance attr / return tuple from
  `respond()`): a tuple breaks every caller; a mutable attr races across
  concurrent messages. The prefix sentinel reuses the established protocol
  and dies at the handler boundary.
- **Keeping a separate Toob-flavored text wrap function**: two prompts to
  keep in sync for one persona. One function, two transports.

## Validation

- `tests/unit/test_music_text_wrap.py` (rewritten): text branch calls the
  Toob wrap with the sentinel; history stays clean; `[SILENT]` untagged;
  `[SPEAK]` tagged in text but never voice; Toob prompt contract (persona,
  music-only label, no deal vocabulary, ≤40-token cap, raw fallback);
  `_split_persona` strip/default/mid-string cases; `speak_if_in_channel`
  persona dispatch (toob/default/unknown-falls-back); deal wrap untouched.
- `tests/unit/test_agent_music_reply_flow.py`: end-to-end — posted text has
  no sentinel, VC speak gets `persona="toob"`, plain chat stays Poob.
- Post-deploy: a text play request should produce a Toob-venom reply in
  chat, `Spoke chat response in VC persona=toob` in the log, and Enceladus
  (not Fenrir) in the `TTS synthesized` line.

## Rollback

Two-line revert in `_handle_music`'s text branch (drop the sentinel prefix,
call any wrap); `_split_persona` degrades to a no-op ("poob", unchanged)
when no sentinel arrives.
