---
type: research
status: active
date: 2026-07-17
tags: [voice, music, brain, routing, deepgram, reliability, census]
related: [[wake-utterances-lost-to-deepgram-miss-salvage]] [[music-search-candidate-rerank]] [[control-command-misroute-by-weak-rung]] [[deepgram-zombie-stream-no-transcript]] [[voice-hallucination-drop-gets-a-line]]
---

# Full-log failure census (2026-07-08 → 07-17): why the bot feels "on and off"

## Method

Operator complaint: *"it's so on and off — it works and then it doesn't.
Every time we fix an issue another one comes up."* Instead of chasing the
latest symptom, a multi-agent census classified **every addressed
interaction** in 9 days of prod logs (28,583 lines, 6 session windows,
225 interactions), split PRE vs POST the 2026-07-13 17:38 deploy
(`ef78cc0`), and cross-referenced every failure class against a 41-item
ledger of the vault's past fixes and their explicitly-deferred edges.

## Headline findings

**The deploy's fixes all verified working in prod** — zero post-deploy
occurrences of: loop blind-cycling, autoplay misroutes, executed control
misroutes ("max volume"→skip was CAUGHT by the new override at 07-16
01:55:36), content-free-address tool hallucinations, or hallucination-drop
dead air. Routed-interaction quality improved (W5: 27/35 routed OK-or-saved).

**But the overall experienced failure rate stayed ~constant** (pre: 67/133;
post: 54/92) because three planes the deploys never touched became dominant.
That is the mechanism of "every fix surfaces another bug": the planes fail
in alternation, and the deepest one (transport) is invisible to all routing
work.

## Root causes by post-deploy frequency

1. **Deepgram stream death — 35 events, 38% of ALL post-deploy addressed
   attempts.** Wake fires, transcript never arrives, request silently eaten.
   Three vault generations fixed *stream recovery* but always dropped the
   utterance. **FIXED this round**: utterance salvage via fallback STT
   ([[wake-utterances-lost-to-deepgram-miss-salvage]]).
2. **STT transcript corruption — 8 events.** "Poob, skip" heard as "It
   pooped skip" (discarded; user: *"Ben, fix your bot"*), wake tokens
   dropped, utterances truncated. Keyterm boosting for "Poob" is ALREADY
   wired (`keyterm=Poob` in the stream URL — the census recommendation was
   already implemented); remaining levers are observability (**FIXED**:
   addressed-transcript logs no longer truncate at 100 chars — the census
   itself was blinded by that cut) and the still-queued wake-word v4 retrain.
3. **Sub-900s junk search results — 5 events.** Funko unboxing 11:55, ICE
   press conference 10:15, app-store tutorial 7:33 — all under the re-rank's
   duration gate. **FIXED**: `_JUNK_TITLE_MARKERS` trigger + −0.6 penalty
   ([[music-search-candidate-rerank]] addendum).
4. **Primary-rung (Groq) fabrication/truncation — 4 events.** Same utterance
   routed `query='home or let the barts out'` at 00:46 and `query='home'`
   at 02:06 (wrong song). **FIXED** (truncation shape): `_prefer_full_play_span`
   router-truncation guard — routed query that is a strict substring of the
   user's spoken play span gets extended to the full span. The sung-lyric-tail
   fabrication residual ("Alabama, Arkansas" — user was singing along, lyric
   was IN his utterance) is accepted + now attributable via full-transcript
   logs; a lyrics-aware discriminator would be speculative.
5. **Gemini stale-echo play-misroute — 3 events.** "Play Betty Davis eyes.
   Jojo Siwa." → gemini re-emitted its previous `apply_effect/slowed` args →
   wrong action ran SILENTLY, song never played. **FIXED**: symmetric
   misrouted-play override in `_music_safety_net` — explicit play-verb span
   with real song content overrides a non-play music route (never deal;
   control-phrased plays like "play it slower" keep their route via the
   content-token gate).
6. **Safety-net garbage queries — 2 events.** "Play the song. Fuck." →
   literal search → queued "FUCK!! Song! :)". **FIXED**:
   `_trim_trailing_crosstalk` (sentence-boundary trim, abbreviation-safe) +
   content-free spans blank to the existing "Play what?" prompt.
7. **Typing-indicator crash — 1 event.** `async with channel.typing():`
   wrapped the brain call; a Discord 500 on /typing (4 retries, ~25s) killed
   "@Poob play apt apt apt" before the brain ran. **FIXED**: typing is now
   fire-and-forget (`trigger_typing()` best-effort).
8. **[SILENT]-ack opacity (amplifier).** Successful toggles are silent (by
   design), so users repeat commands and can't distinguish a fixed system
   from a broken one; misroutes corrected silently are equally invisible.
   **DEFERRED** (see below).

## Deferred this round (documented, evidence-first)

- **Proactive Deepgram zombie watchdog** (frames-sent vs results-received):
  the vault's own deferred detector whose revisit trigger has now fired —
  but salvage converts its absence from *request-loss* to *~1.5–2s extra
  latency*, sharply lowering urgency. Revisit if salvage rates stay high.
- **Salvage-failure spoken feedback** ("say that again" when even salvage
  yields nothing): requires TTS plumbing into the dual pipeline; residual
  after salvage should be near-zero. Revisit with post-deploy data.
- **Audible-on-anomaly acks** (speak when an override corrects a misroute):
  UX-policy change needing its own decision note; the silent-success
  contract stays as decided in [[music-text-replies-are-toob]].
- **Gemini rung improvements** (promote above Groq, re-enable alt bucket,
  NVIDIA model swap): capacity/benchmark decisions the vault already holds
  open — operator calls.

## Fix inventory (this round)

| # | Fix | Layer | Tests |
|---|---|---|---|
| 1 | Utterance salvage via fallback STT | dual_pipeline + session | 7 |
| 2 | Junk-title markers in search re-rank | ytdl | 5 |
| 3 | Trailing-crosstalk trim on extracted queries | brain | 4 |
| 4 | Misrouted-play override (stale-echo killer) | brain safety net | 3 |
| 5 | Router-truncation guard (`home` ⊂ full span) | brain play branches | 2 |
| 6 | Content-free span → "Play what?" | brain safety net | 2 |
| 7 | Typing indicator fire-and-forget | agent_handler | (flow tests) |
| 8 | Full addressed-transcript logging (100→500) | session | — |

All shipped together; census artifacts (window classifications, ledger,
synthesis) live in the session transcript for wf_45860db7-f20.
