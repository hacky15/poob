---
type: moc
status: active
tags: [index]
---

# Incidents — Map of Content

Post-incident reviews. Bug reports that required investigation, a root-cause, and a fix get a note here regardless of whether a user noticed.

Each note follows the pattern: Symptom → Root cause → Fix → Validation → Follow-ups. Keep the symptom concrete (log snippet, user quote) so future search hits it.

Resolved incidents stay `status: resolved`. Open ones stay `status: active`. If a fix regresses or a near-miss recurs, open a new incident and link back.

## Entries

### 2026-09

- [[voice-reasoning-model-token-starvation-on-real-questions]] — operator asked Poob a real question twice in VC, got "got nothing to say right now" both times. Reproduced live, deterministic 8/8: qwen3.8-27b (voice_llm_model) spent its ENTIRE 200-token production budget on hidden reasoning for a genuine question (200-300 reasoning tokens needed), leaving zero for the answer — the prior eval only tested low-effort banter prompts, never a real question. Fixed: max_tokens_voice/voice_llm_max_tokens 110/200→1000, all toob_max_tokens ceilings 100→400; new tests check the previously-untested floor, not just the ceiling (2026-09-09, resolved)
- [[nvidia-cerebras-account-entitlement-gaps]] — NVIDIA and Cerebras rungs classified as "stale model id" were actually account-level: NVIDIA's key had zero working models (platform-wide EOL sweep + missing entitlement for the new lineup), Cerebras hits 402 Payment Required on every model. Also found and fixed a real bug: CerebrasProvider.is_available() said True even when auto-detection had just proven every model broken. NVIDIA re-provisioned + `nemotron-3.5-lightning-30b-a3b` chosen after a 3-candidate live eval (correctness + no reasoning-leak beat gpt-oss-20b and nemotron-3-super-120b). Cerebras still open (2026-09-08, active — Cerebras billing outstanding)
- [[stt-cooldown-shipped-inert]] — a PR claiming to fix Gemini STT 429 storms declared an empty cooldown dict and a new exception type, but the existing generic `except Exception` handler already produced identical behavior with or without the change — the cooldown was never checked or set anywhere. Zero tests shipped with it. Fixed by extracting the LLM cascade's proven rate-limit breaker into a shared registry rather than re-hand-rolling one (2026-09-08, resolved)
- [[routing-tpm-ceiling-degrades-cascade]] — the common denominator behind "brain glitched" (25x), wrong/slow effects, and 15 misrouted plays: every route costs ~3,100 tokens against Groq's 8k TPM, so ~2.6 routes/min, after which 20% of routes fall to the Gemini rung documented to hallucinate actions, and behind it FIVE configured model ids are dead (verified against live catalogs). Four prior fixes trimmed the per-call cost but never the structure (2026-09-06, active — voice model split shipped, dead models + freshness check outstanding)
- [[gateway-keepalive-result-block-no-recovery]] — a routine voice auto-join was followed 60.15s later by py-cord's own "stopped responding to the gateway" warning, then TOTAL silence for 6+ minutes (voice, text, patrol) with the process alive and idle — confirmed py-cord's own recovery (`KeepAliveHandler.run()`) blocks its thread forever on an unbounded `f.result()` if the socket close never completes. Fixed: `GatewayWatchdog`, an asyncio-independent thread that force-exits the process (container `unless-stopped` policy recovers) if the gateway goes silent past threshold — mirrors the patrol scheduler's existing wedge-recovery pattern (2026-09-05, resolved)

### 2026-08

- [[query-extension-defeated-duplicate-play-dedup]] — "play crank that by pickle" queued twice 16s apart: the router-truncation guard extended the second request's query ('...Oh, yeah, dude') AFTER dedup had already keyed on the un-extended first, so a byte-identical routed retry never matched. Fixed: dedup keys on the query as originally routed, captured before extension, at both call sites (`_handle_music`, `_handle_music_voice_streaming`) (2026-08-31, resolved)
- [[voice-llm-model-deprecated-and-never-wired]] — Groq removed `llama-3.1-8b-instant` from its catalog entirely (confirmed live via models.list()); AppConfig.voice_llm_model existed for exactly this swap but was never passed into PoobBrain, so 6 call sites hardcoded the dead literal directly. The naive fix (swap to gpt-oss-20b) almost shipped a SECOND bug: it is a reasoning model that silently returns empty content at realistic token budgets (measured 0-100% empty depending on config) unless `reasoning_effort="low"` is set. Found live, mid-outage, by reading production logs (2026-08-26, resolved)

### 2026-07

- [[concurrent-builds-race-latest-tag]] — PRs #6 and #7 merged 3 seconds apart; both builds pushed `:latest` concurrently and the OLDER commit finished last, so the tag pointed at code missing the newer merge while main said both were shipped. Every step reported success. Fixed: `concurrency` group on build.yml with `cancel-in-progress: false` (a deploy job must not be interrupted mid-push) (2026-07-29, resolved)
- [[effect-clear-suppressed-by-normal-guard]] — "Put the bass back to normal" got a persona joke and the ultrabass stayed on for 45 min; the user never retried. An OVER-CORRECTION from [[normal-volume-routed-to-filter]]: its guard clause ("do NOT fire just because the word 'normal' appears") suppresses genuine effect-clears, and `_CONTROL_OVERRIDES` has no effect-clear entry to catch the fall-through. Found by pre-deploy baseline audit, not a user report (2026-07-27, resolved — deterministic override + narrowed prompt clause; the blunt 'normal' guard from 2026-06-09 replaced with a precise one that is also SHORTER)
- [[voice-session-never-reestablished-mid-flight]] — poob went deaf in a guild for **73+ hours** and nothing said a word: a voice WS 1006 exhausted py-cord's retry loop, and `restore_sessions()` only ever runs once at boot, so persisted intent and reality diverged with no loop comparing them. Found by audit, not by users. Fixed: a 2-min `VoiceCog` presence reconciler that repairs AND logs the divergence (2026-07-21, resolved)
- [[referential-play-query-searched-literally]] — "play the music that we told you to play" queued Gorillaz, then Shannon: a play span whose only surviving token was the pronoun `we` passed the content-free gate and got literal-searched. Structural cause: the same lexicon existed as three divergent copies. Fixed: one shared lexicon + the closed function-word class (2026-07-22, resolved)
- [[play-question-misrouted-to-play-command]] — "do you think we should play hard or standard?" (a BTD6 opinion question) played a random guide video: the ENTIRE LLM cascade correctly declined, then the deterministic `_music_safety_net` play backfill REVERSED that correct decision on bare substring match. Fixed: `_looks_like_non_music_play_usage` guard (opinion-question + game-reference + figure-of-speech patterns) checked before both play-related overrides. Confirms the risk [[music-routing-prompt-thoughtfulness]] predicted in 2026-06 (2026-07-21, resolved)
- [[wake-utterances-lost-to-deepgram-miss-salvage]] — the #1 user-visible failure by census count (~19 eaten requests in the 07-17 session alone): every Deepgram wake-miss still DROPPED the utterance — three generations of zombie fixes only healed the stream for the next attempt. Fixed: always-on per-user 16k PCM salvage ring + fallback one-shot STT + re-imposed text wake gate; the request is served ~1-2s late instead of eaten (2026-07-17, resolved)
- [[now-playing-card-snapshot-after-brain-call]] — the now-playing card stopped posting for text play requests: the `track_before` snapshot ran AFTER the brain call (contradicting its own comment), so the track that started mid-call was the baseline and the change-poll timed out. Fixed by snapshotting before the call; the poll helper unchanged (2026-07-12, resolved)
- [[music-text-wrap-inherited-deal-instructions]] — (backfilled note) text music replies narrated absent deal categories because the text path reused the deal wrap; fixed 2026-07-03 with a music-only Poob text wrap, later superseded by the Toob wrap (2026-07-03, resolved → [[music-text-replies-are-toob]])
- [[spotify-track-link-play-dead-end]] — pasted Spotify track link → {play, url:…} → "play what?" ("Yeah. It didn't work."): the play path only read `query`, the resolver was playlist-only, and a Spotify playlist URL via play would trap in the YT extractor. Fixed: url→query promotion at both brain gates, `resolve_track` metadata→YT-search, playlist links delegate to the shared Spotify flow (2026-07-11, resolved)
- [[bare-wake-address-routes-hallucinated-tool]] — a bare "Hey, Poob." (STT dropped 9.7s of speech) routed to {autoplay, on}, echoing a request from 17 min earlier, acked silently so the user heard nothing. Fixed: content-free addresses skip tool routing entirely and get a casual reply (2026-07-11, resolved)
- [[control-command-misroute-by-weak-rung]] — "max volume" skipped the song (and "bass boost" applied *slowed*): gemini-flash-lite hallucinates actions for terse control commands, and the deterministic net couldn't catch it — it only ran when NO tool was routed and couldn't see past the voice wake prefix. Fixed: unified wake/attribution-aware control override (stop/skip/volume/autoplay/loop) run unconditionally on every routing result (2026-07-11, resolved)
- [[autoplay-request-enables-loop-one]] — "turn autoplay on" silently enabled LOOP_ONE (same song forever, new requests never played); three stacked defects: the `loop` handler ignored its `mode` arg and blind-cycled, the router had no autoplay/loop examples so gemini-flash-lite collapsed one into the other, and the schema gave loop no way to express a target. Fixed: handler honors the target (cycle only for the bare button), routing rules + schema distinguish autoplay from loop, deterministic safety net for the exact phrases (2026-07-11, resolved)

### 2026-06

- [[cascade-outage-nvidia-hang-gemini-rpm]] — ~15.5s to first word on every VC turn: Groq daily-capped + Gemini burst-throttled (20 RPM/model) + Scout capped + NVIDIA NIM hung; each turn paid the full 15s REST timeout re-probing the hung rung. Fixed: consecutive-timeout ejection (2 → 120s cooldown), routing timeout 15s→6s, Gemini body/"retry in" parsing, second Gemini model rung. Also surfaced: routing prompt ≈4k tokens/call → only ~50 Groq turns/day (2026-06-09, resolved)
- [[normal-volume-routed-to-filter]] — "normal volume" hijacked to apply_effect (Gemini even hallucinated super_slowed); bare 'normal' had been added as an effect-clear trigger by the effect-off fix. Fixed: principled volume-vs-effect split in the routing prompt (2026-06-09, resolved)
- [[groq-daily-cap-routing-storm]] — back-to-back questions took 22-23s and serialized (the "was slavery good" cluster); Groq's 200k-TPD exhausted and the cascade re-probed the capped model every turn with SDK retry-backoff. Fixed: fail-fast client + provider circuit-breaker driven by the 429's own retry-after (2026-06-08, resolved)
- [[wake-address-hey-dropout]] — operator's "Hey Poob, play X" got no response (his audio was fine); Deepgram dropped/mangled the "hey" lead-in ("A Poob", "Apoob", "Poob") so the hey-required text regex rejected 6 real addresses as spurious. Fixed: second deterministic "poob-opener + command-verb" matcher; spurious-fire rejection kept load-bearing (2026-06-04, resolved)
- [[voice-4014-reconnect-event-loop-wedge]] — voice WS 4014 force-disconnect → DAVE MLS re-key ran unlocked on the shared event-loop thread → native `davey` FFI wedge froze voice + text + patrol for 8 min; recovered by restart. Root: the serialization lock dropped in the Pycord migration (2026-06-01, resolved — lock + executor offload confirmed shipped 2026-09-05, status had gone stale)

### 2026-05

- [[text-mode-rlhf-refusal-leak-2026-05-29]] — Poob answering as bland RLHF assistant in Discord text: non-empty gpt-oss routing text (refusals + markdown) returned verbatim; the 2026-05-06 casual fallback only covered the empty case (2026-05-29, resolved)
- [[wake-word-v3-deploy-rename-and-rebake]] — wake-word v3 didn't actually go live after env-flip; three stacked bugs (wrong env-var name, volume-shadow path, model missing from image) (2026-05-20)
- [[c-drive-docker-data-vhd-trap-2026-05-14]] — C: filled to 1.69 GB during wake-word v3 builds because Docker's data VHD stayed at default while only the engine distro had been relocated (2026-05-14)
- [[auto-join-missed-listening-setup]] — text-channel music auto-join skipped STT + wake-word setup; bot played music but couldn't hear voice commands (2026-05-09)

### 2026-04

- [[dave-timeout-fail-hard-regression]] — agent flipped DAVE-not-ready into a fatal disconnect; auto-leave on every /join until reverted (2026-04-29)
- [[voice-three-failure-modes-april27]] — `/join` crash + empty-query play + casual leak of tool-name (2026-04-27)
- [[casual-chat-response-13s-latency]] — casual voice reply took 13.5 s end-to-end; cloud LLM cascade thrashed by 429s; resolved via Groq primary swap + Cerebras drop + casual-bypass (2026-04-23, resolved)
- [[wake-gate-stt-mishear-rejection]] — wake gate rejected "play X" when STT mis-heard "Poob" as "poop" / "boop" / etc.; phonetic allowlist landed in address-detector, dual-pipeline command-verb override shipped 2026-06-04 (2026-04-23, resolved → [[wake-address-hey-dropout]])
- [[music-tool-hallucination]] — LLM played "Bad Guy" from 20-minute-old context
- [[wake-gate-over-rejection-during-music]] — wake gate rejected legit addresses while music was playing
- [[voice-addressee-confusion]] — Poob called Ben "lab rat" and Jeweinery "lab rat"
- [[slash-command-sync-on-ready]] — `/join` silently unregistered; Pycord sync fired before cogs loaded

### 2026-03

- [[graphql-amount-with-offset-cents-bug]] — $18.50 listings appeared as $1850 (cents treated as dollars)
- [[detail-enrichment-empty-og-jsonld]] — detail enrichment returned empty data on every listing
