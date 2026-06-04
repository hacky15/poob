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

### 2026-06

- [[wake-address-hey-dropout]] — operator's "Hey Poob, play X" got no response (his audio was fine); Deepgram dropped/mangled the "hey" lead-in ("A Poob", "Apoob", "Poob") so the hey-required text regex rejected 6 real addresses as spurious. Fixed: second deterministic "poob-opener + command-verb" matcher; spurious-fire rejection kept load-bearing (2026-06-04, resolved)
- [[voice-4014-reconnect-event-loop-wedge]] — voice WS 4014 force-disconnect → DAVE MLS re-key ran unlocked on the shared event-loop thread → native `davey` FFI wedge froze voice + text + patrol for 8 min; recovered by restart. Root: the serialization lock dropped in the Pycord migration (2026-06-01, active — fix planned)

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
