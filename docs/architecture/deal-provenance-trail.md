---
type: architecture
status: active
date: 2026-03-28
tags: [deal, notifications, observability]
related: [[vlm-triage-pipeline]] [[vlm-cascade-operational-findings]]
---

# Deal provenance trail — evaluation details in notifications

## Purpose

Each deal notification includes an "Evaluation Details" section showing which models/services contributed to the decision. Operator can tell at a glance whether the evaluation was Groq-solo, panel-consensus, or a last-ditch Ollama read.

## Data flow

- `DealProvenance` dataclass accumulates state through the pipeline: triage → enrichment tier → price source → VLM providers/confidence → score adjustments.
- Serialized to JSON and stored in `deals.provenance_json`.
- Rendered as compact text in the Discord embed (Evaluation Details field).

## Key design decisions

- **Single flat dataclass** with stage prefixes (`vlm_*`, `price_*`) — not per-stage objects. Keeps serialization straightforward and avoids nested field paths.
- **VLMCascade exposes `last_responding_providers`** — set after each `invoke()`; provenance reads this rather than threading provider metadata through every call.
- **Web search results captured in provenance, NOT in reasoning** — prevents search garbage from polluting VLM reasoning display. Earlier the pipeline appended `[Web: ...]` to reasoning for diagnostics; removed.
- **Score adjustments recorded as strings** — e.g. `"incredible→great: $28 saved, 55%"`. Human-readable; no need to re-derive reasons at render time.
- **Backward compatible** — old deals without `provenance_json` show no Evaluation Details field.

## Invariants

- Never append raw web search text to VLM reasoning. Web search usage is tracked in `DealProvenance` instead.
- If a new pipeline stage wants to contribute context, add a new `DealProvenance` field with the appropriate stage prefix — don't retrofit existing fields.
