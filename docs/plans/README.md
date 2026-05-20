---
type: moc
status: active
tags: [index]
---

# Plans — Map of Content

Per-feature implementation plans written before coding. A plan names the goal, sketches the approach, calls out the tradeoffs, and lists the files likely to change.

Plans are not permanent. Once the feature ships, the plan can be archived — the decisions that came out of it belong in `decisions/`, the incidents that arose belong in `incidents/`, and the surviving architecture is documented in `architecture/`.

## Entries

### Active

- [[wake-word-v4-phonetic-neighbor-followup]] — close v3's `Hey Noob/Tube` + bare-stem gaps via phrase-weight reshaping (2026-05-16)
- [[voice-latency-optimization]] — VAD + STT + TTS improvements; some items shipped, others open

### Superseded / historical

- [[disnake-voice-receive]] — rejected in favor of Pycord + `voice_compat`; see [[voice-architecture]]
- [[phase-next-implementation-plan]] — spring 2026 pipeline-robustness plan; landed via [[unified-filter-pipeline]]
