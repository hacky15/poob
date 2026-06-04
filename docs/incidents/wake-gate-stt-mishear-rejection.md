---
type: incident
status: resolved
date: 2026-04-23
tags: [voice, wake-word, dual-gate, brain]
related: [[wake-word-dual-gate]] [[wake-gate-over-rejection-during-music]] [[wake-word-v3-shipped]] [[wake-address-hey-dropout]]
---

# Wake-gate rejects commands when STT mis-transcribes "Poob" as "Poop" / "Boop" / etc.

## Resolution (2026-06-04)

**Option B shipped** at the dual-pipeline text-gate layer — see [[wake-address-hey-dropout]]. A second "command address" recognition path (poob-variant opening the utterance + a word-boundaried imperative verb) now recovers real addresses where STT drops/mangles the "hey" lead-in, while the load-bearing spurious-fire rejection stays intact. Validated by adversarial regression over 5h of production logs (6/6 real misses recovered, 0 new false positives, 53 prior accepts preserved). Marking `resolved`; the sections below are the original diagnosis, kept for context.

## Current state (2026-05-24 audit)

Partially mitigated. Option A from §"Fix (proposed, not yet implemented)" shipped at the address-detector layer — `_WAKE_WORDS` in [voice/address_detector.py](../../src/poob/voice/address_detector.py) accepts the broad phonetic family (`poob|poop|boob|boop|hoob|noob|goop|pewb|...` plus a catch-all `p[ou]{1,2}[bp]e?`). However, the dual-pipeline gate that emitted the original "Audio wake word overridden by text" rejection still uses a narrower `_TEXT_WAKE_RE` at [voice/dual_pipeline.py:590](../../src/poob/voice/dual_pipeline.py#L590) which requires a literal `hey ` prefix before any phonetic stem — so the original-symptom transcript "They poop. Play..." would still miss at the dual-pipeline layer specifically.

Whether this produces a user-visible rejection now depends on whether the session-level `MultiSignalAddressDetector` (which uses the broader allowlist) elevates the utterance from passive → addressed before the response pipeline gives up. That's not something we can know from static reading; it needs a live reproduction.

The original 13.5 s symptom this was filed with is gone post-[[groq-gpt-oss-20b-swap]], so the practical urgency is low. Option B (audio_match + command-verb override) remains unshipped and is deferred until after Silero VAD [[voice-latency-phase1-silero-reenabled]] is validated in a real voice channel — stacking voice-gate changes on un-validated VAD is the wrong order.

Keeping `status: active` so the unaddressed delta is visible.


## Symptom

User (Ben) said "Hey Poob, play the jah jah jah blah blah blah Bass jackers remix" at `2026-04-24T01:29:24Z`. Audio wake-word detector fired correctly (`Wake word detected latency_ms=684`). But the Deepgram transcript came back as:

```
"They poop. Play the jaw jaw jaw blah blah blah. Bass jackers remix."
```

Log line:
```
Audio wake word overridden by text (no match in transcript)
  transcript='They poop. Play the jaw jaw jaw blah blah blah. Bass jackers'
  user='Ben (hacky15)'
```
Followed by `wake_word=False` on the utterance — **bot silently ignored the play request.**

User retried ~50 seconds later with clearer speech: "Hey, Poob. Play jah jah jah blah blah blah bass jackers remix." — STT heard "Poob" correctly that time, `wake_word=True`, music played.

## Root cause

The v2 context-aware wake gate ([[wake-word-dual-gate]]) requires BOTH acoustic detection AND literal text-match for "poob"/"hey poob" in the transcript when `bot_audio_active()` is True (anti-loopback rule). When STT mis-transcribes "Poob" phonetically similar but lexically different ("poop", "boop", "booper", "boob"), the text-match check returns false and the rule lands in the final `else: NOT addressed` branch.

This is the exact inverse of the original loopback problem that v2 was designed to fix — v2 was too permissive (music lyrics → hallucinated wake), it's now too strict (real speech → rejected because STT was lossy).

## Fix (proposed, not yet implemented)

Two viable options for the text-match layer in the wake-gate:

**Option A: Phonetic near-match allowlist.** Extend the text-match to accept "poop", "poo", "boop", "booper", "boob", "pub" in addition to "poob" / "hey poob". These are the common STT failure modes for the wake phrase and rarely appear in other contexts. ~10 lines.

**Option B: Command-verb override.** When `audio_match=True` AND the transcript contains an imperative verb like `play`, `skip`, `pause`, `resume`, `stop`, `volume`, `queue`, skip the text-match requirement. The user is clearly issuing a command; acoustic detection is sufficient.

Option B is cleaner — it generalizes beyond the wake-phrase mistranscription class to any STT lossiness where the user's intent is clear from the verb. Option A is a tighter, more surgical patch.

Recommend **Option B** as the primary fix, with Option A as a tight allowlist on the remaining general-chat path.

## Validation

After the fix lands:
- Reproduce by speaking "Hey Poob, play X" in a voice channel while another audio source is playing (the loopback-risk condition that triggers v2's strict rule).
- Confirm log shows `wake_word=True` even when Deepgram transcribes "poop" / "boop".
- Confirm the original loopback-rejection behavior still works: play music with lyrics containing "blah blah", confirm no ghost wake triggers when the user is silent.

## Follow-ups

- Consider adding a `wake_gate.stt_mishear_overridden` structured log line so future mis-transcription events are measurable, not invisible.
- The STT keyterm boosting (`keyterm=Poob`) was added specifically to help Deepgram hear the wake phrase. It clearly still fails on casual fast speech. Worth investigating whether adding phonetic variants as keyterms (`poop`, `boop`) would help — though those also appear in music/gaming contexts so they might over-prime in the loopback direction.

## Out of scope

This incident is diagnosed but **not fixed** in the scanner-chat session. The fix belongs in [[voice-architecture|voice/voice_compat.py or voice/dual_pipeline.py]] and is assigned to the brain/voice chat.
