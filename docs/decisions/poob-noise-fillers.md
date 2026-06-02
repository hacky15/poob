---
type: decision
status: active
date: 2026-06-01
tags: [voice, tts, filler, latency, persona]
related: [[voice-latency-phase2-filler-dispatch]] [[voice-architecture]] [[voice-latency-phase3-kokoro]]
---

# Latency fillers are Poob-voice thinking-noises, not stranger-voice words

## Context

The Phase 2 latency filler ([[voice-latency-phase2-filler-dispatch]]) plays a quick clip the instant Poob is addressed, masking the LLM+TTS delay before the real reply. As shipped it spoke word-phrases ("Hmm…", "Let me think…", "One sec…") synthesized with **Edge TTS** (`en-US-RogerNeural`) — a completely different voice from Poob's Google Chirp3-HD **Fenrir**. In production the operator heard it as a stranger interrupting before Poob spoke, and asked: was Edge chosen for speed?

Key finding: **no.** Fillers are pre-generated once at startup and cached as MP3s; at runtime `_maybe_play_filler` just reads a cached file off disk — there is **zero TTS generation in the hot path**. So the generating voice has no effect on runtime latency (Edge and Google clips play equally instantly). Edge was chosen for convenience (free, no API key, trivial one-time batch), not speed. There is therefore no latency reason for the voice mismatch.

## Decision

1. **Content:** replace the word-phrases with quick, purposeful Poob thinking-**noises** — `"Hmmmm."`, `"Aughh, yeah, aughh."`, `"Mmmnghh."`, etc. (`FILLER_PHRASES` in `voice/fillers.py`). Spelled to coax a vocalization rather than a spelled-out reading.
2. **Voice:** generate the clips in **Poob's own Google voice** (`voice_google_tts_voice`, Fenrir) by default, so the noise sounds like Poob. `generate_fillers` is now provider-agnostic: it takes a `TTSProvider` (main passes a `GoogleCloudTTS` Fenrir instance) and falls back to Edge per-clip if Google is unavailable. Runtime playback is unchanged (cached file → instant).
3. **Cache correctness:** clips are cache-keyed by a `(voice, phrase)` hash in the filename; changing the voice or the phrase list regenerates, and stale clips are pruned from `data/voice_fillers/` (so the old word-fillers don't linger on the volume).
4. **Operator knobs (test without code changes):** `voice_filler_enabled` (default True) toggles the whole feature; `voice_filler_voice` (default empty → Fenrir) overrides the filler voice via env.

## Alternatives considered

- **Keep Edge but pick a deeper voice.** Cheaper but still not Poob; the mismatch problem remains. Rejected.
- **Generate fillers live per-response in Poob's voice.** Would add real TTS latency to the hot path — the exact thing the filler exists to hide. Rejected; cached is correct.
- **Config-driven phrase list via env.** Deferred — phrases are a code constant (one-line edit); a comma/JSON env list adds parsing complexity for little gain. The voice knob covers the main tuning need.

## Consequences

- The "thinking noise" now sounds like Poob, removing the stranger-voice jarring effect, at **no latency cost** (still a cached-file play).
- **Open quality question to validate in prod:** whether Google Chirp3-HD Fenrir renders `"aughhh"`/`"hmmmm"` as a believable moan vs. something flat. This is a test-after-deploy item; if it lands poorly, flip `voice_filler_voice` to a different voice or tweak `FILLER_PHRASES` — no code change needed for the voice.
- A one-time ~8-clip Google TTS call at each boot where the cache is cold (negligible quota; idempotent thereafter).
- Old `data/voice_fillers/filler_00..07.mp3` word-clips are pruned on first run of the new code.

## Update 2026-06-02 — workshopped: per-phrase rate + weights, and a separate join-noise pool

The "open quality question" above was resolved by an operator listening session (synthesizing candidate spellings through real Fenrir and rating them). Findings:

- **Fenrir spells out some repeated-letter combos** (`"Hmmmm"` → "h-m-m-m", `"Aargh"`, and — critically rate-dependent — `"rrraugh"`/`"ughhh"` spell out at 0.85 even though they render fine at 1.25). **Real-word fillers** ("Welp", "Whoa", "Yeah yeah") and **pure `mmm`** were all rejected. Winners are vowel-heavy moan clusters: `Aughhhh`, `Auugh`, `Auuughhh`, `Aaaughhh`, `Ohhh(hh)`, plus a few quick utility noises (`Mm-hmm`, `Uhh`).
- **Slower = moanier.** The best moans render at **0.7–0.85**, not Poob's 1.25 speaking rate. So `FILLER_PHRASES` is now `(phrase, rate, weight)` — each clip synthesized at **its own** rate (`generate_fillers` takes a rate-aware `synth_factory`; cache key is now `(voice, rate, phrase)` so the same spelling at two rates is two clips). `FillerPlayer` does **weighted** selection (`random.choices`), so the hero `"Aughhhh."` surfaces ~30%.
- **Join "catchphrases" are a separate weighted pool**, NOT latency fillers. `session.play_entrance()` previously synthesized ONE hardcoded line (`"Its poob here, auuuuuughhhhh yeahhhhhhhh"`) live on join. It now picks weighted-randomly from `JOIN_PHRASES` (cringe 2–3 word quip + an approved moan tail, e.g. "Daddy's home, ohhh yeah." ~25%, "Poob has arrived, aaaughhh." ~10%) and synthesizes in Fenrir at `JOIN_RATE = 0.85` via the new `_synthesize_at_rate` helper (same build-a-rate-specific-Google-synth pattern as Toob/Boob). Played only on a real entrance (`play_entrance=True` — `/join`), never on music auto-join or restart auto-rejoin.
- **Hazard (durable):** never put a moan tail that spells out into a join phrase. `rrraugh`/`ughhh` spell at 0.85; use `aughh`/`auugh`/`ohhh`/`aaaughhh`/`auuughhh`/`uuughhh` (verified to vocalize). Test `test_join_phrases_dropped_spelled_out_tails` guards this.

Tests: `tests/unit/test_fillers.py` (rate/weight shape, rate-keyed cache distinctness, weighted selection, bare-Path back-compat, join-pool weighting + fixed-tail guard).
