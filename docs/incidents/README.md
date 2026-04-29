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

### 2026-04

- [[voice-three-failure-modes-april27]] — `/join` crash + empty-query play + casual leak of tool-name (2026-04-27)
- [[music-tool-hallucination]] — LLM played "Bad Guy" from 20-minute-old context
- [[wake-gate-over-rejection-during-music]] — wake gate rejected legit addresses while music was playing
- [[voice-addressee-confusion]] — Poob called Ben "lab rat" and Jeweinery "lab rat"
- [[slash-command-sync-on-ready]] — `/join` silently unregistered; Pycord sync fired before cogs loaded

### 2026-03

- [[graphql-amount-with-offset-cents-bug]] — $18.50 listings appeared as $1850 (cents treated as dollars)
- [[detail-enrichment-empty-og-jsonld]] — detail enrichment returned empty data on every listing
