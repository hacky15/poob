---
type: research
status: active
date: 2026-06-09
tags: [llm, free-tier, routing, cerebras, gemini, groq, models, audit]
related: [[gemini-tool-router-rung]] [[groq-gpt-oss-20b-swap]] [[groq-daily-cap-routing-storm]] [[drop-cerebras-from-cascade]]
---

# Free-tier LLM lineup audit — June 2026

Cited audit of whether our $0 model lineup is current/optimal. Source: a
deep-research pass (22 sources → 94 claims → 25 adversarially verified, 12
confirmed). The pass hit a session limit before the synthesis step, so the
recommendations below are assembled from the **confirmed claims** + our own prod
evidence. Items marked ⚠️ are unconfirmed (verifier abstained on the limit) and
need a quick check before relying on them.

## Verified findings (June 2026)

- **Cerebras free tier = 1,000,000 tokens/day per model** — 5× Groq's 200k TPD.
  Source: [inference-docs.cerebras.ai/support/rate-limits] (`gpt-oss-120b | 5 | 30K | 1M | 1M`),
  corroborated by getaiperks ("permanent free tier, no credit card"). [3-0]
- **Cerebras free exposes only `gpt-oss-120b` and `zai-glm-4.7`** — no small fast
  model like gpt-oss-20b. [2-0]
- **Cerebras is FAST**: `gpt-oss-120b` ~1,684–1,846 tok/s, **TTFT ~0.53–0.59s**
  → viable for low-latency voice routing. Source: artificialanalysis.ai. [3-0]
- The claims "Cerebras 5 RPM **limits usability**" and "Cerebras tool-calling
  **lags**" were both **REFUTED** (0-2) — i.e. neither weakness is supported.
  (The rate table does list 5 RPM / 30K TPM, but the *limiting conclusion* didn't
  survive verification — ⚠️ still worth a real burst test before trusting it for
  rapid back-to-back voice.)
- **Gemini 3.5 Flash — published 19 May 2026** ("next iteration in the Gemini 3
  series, natively multimodal, reasoning"). Source: deepmind model card. [3-0]
  Newer than our routing fallback (2.5-flash-lite) AND our vision rung
  (3-flash-preview).
- **Gemini 3.5 Flash is #1 for tool-calling** on LLM-Stats (42.7, ahead of Claude
  Opus 4.8 42.2) [2-1]; agentic benches MCP Atlas 83.6%, Toolathlon 56.5% [3-0].
- **Gemini free-tier model list CONFIRMED** (Google pricing page, dated
  2026-05-06, via follow-up search 2026-06-09): FREE = `Gemini 3.1 Flash-Lite
  Preview`, `3.1 Flash Live Preview`, **`Gemini 3 Flash Preview`**, `2.5 Pro`,
  `2.5 Flash`, `2.5 Flash-Lite`. **Pro models removed from free on 2026-04-01.**
  **`Gemini 3.5 Flash` (2026-05-19) is NOT on the free list** → do NOT use it on
  the $0 path. Source: ai.google.dev/gemini-api/docs/pricing.
- **Gemini free is REQUEST-capped, not token-capped**: ~1,000–1,500 RPD, 15 RPM,
  1M TPM, no daily-token cap. So it structurally avoids Groq's nightly TPD wall —
  at our volume (dozens of routes/day) we never approach 1,500 RPD. This is *why*
  Gemini is the right fallback for the Groq cap.
- **Groq free per-model token/day caps**: `gpt-oss-20b` & `gpt-oss-120b` = 200K
  TPD, llama-3.3-70b = 100K TPD; 30 RPM, 14,400 RPD org-wide. Source: Groq docs.
- **OpenRouter free vision = Gemma 4 31B, Nemotron Nano Omni 30B, Nemotron Nano
  12B v2 VL** — **no free Qwen3-VL** anymore. [3-0] → our VLM "OpenRouter
  Qwen3-VL" rung is **stale**. Gemma **4** 31B exists (newer than our Gemma 3 27B).
- **Gemini 3 Flash Preview is PAID on OpenRouter** ($0.50/$3 per M) — for $0 use
  Google's own free tier, not OpenRouter. [3-0]
- Our **Groq 200k-TPD** cap is confirmed by our own prod 429s ("tokens per day
  (TPD): Limit 200000"), see [[groq-daily-cap-routing-storm]].

## Measured routing latency (homelab → provider, our tool schema, 2026-06-09)

Real benchmarks from inside the poob container (network + tool-call generation
included — i.e. true request latency, not TTFT):

| Model | Median | Notes |
|---|---|---|
| Groq `gpt-oss-20b` | **~485ms** (407–534) | fastest free router; the practical floor |
| Groq `llama-3.1-8b-instant` | ~720ms (665–786) | SLOWER than 20b + routes worse — smaller ≠ faster here |
| Gemini `2.5-flash-lite` | ~714ms | tight, consistent |
| Gemini `3.1-flash-lite-preview` | ~1177ms, **42% of calls >2s** (max 10.7s) | **DISQUALIFIED** — preview is latency-unstable |
| any provider, 429 reject | ~140ms | fast-fail confirmed |

**~200ms average is not achievable on a free cloud tier from a self-hosted box.**
The ~485ms floor is network RTT + tool-call JSON generation, not inference speed
(Groq's LPU generates in tens of ms). A smaller model doesn't help (8b measured
slower). Sub-200ms would need co-located inference = a local model, blocked by
the CPU-only homelab ([[homelab-cpu-only]]). So: we are **already on the fastest
free router that routes correctly** (Groq `gpt-oss-20b`); the only lever is
keeping Groq *available* (the cap is what forces the slower ~714ms Gemini rung).

`3.1-flash-lite-preview` routes *better* on quality (correctly treated "who sings
this" as casual where `2.5-flash-lite` hallucinated a play) but its 42% >2s spike
rate kills it for voice. Revisit when a STABLE (non-preview) 3.x flash-lite ships.

## Per-role verdict

| Role | Now | Verdict | Why (cited) |
|---|---|---|---|
| Routing primary | Groq `gpt-oss-20b` | **KEEP** | fast, free, good tool-calling; cap handled by [[provider-circuit-breaker]] |
| Routing fallback | Gemini `2.5-flash-lite` | **SWITCH → Gemini 3.x flash-LITE** (`gemini-3.1-flash-lite-preview`, already used on our VLM path) | stays in the FREE `-lite` class, newer gen. NOT full 3.5 Flash — that's the #1 tool-caller but the `-lite` tier is what's free; full Flash is likely paid / tiny launch quota. ⚠️ confirm 3.1-flash-lite free RPD covers routing volume |
| Cap-buster rung | — | **SKIP Cerebras** (benchmarked 2026-06-09) | routes correctly but **~1.2s total** (slower than Gemini-lite's 714ms) + **5 RPM confirmed** (429'd under bursts). And unneeded: Groq's cap is token/day; the fallback Gemini-lite is REQUEST-capped (~1,500 RPD ≫ our dozens of routes/day), so Gemini already covers the whole post-cap day, faster. Cerebras's 1M-token budget solves a problem we don't have. |
| Casual voice | Groq `llama-3.1-8b-instant` | **KEEP** | no clearly-better free fast streamer surfaced |
| Deal sub-agent | Groq `gpt-oss-120b` | KEEP model; **consider Cerebras host** | same model on Cerebras = 1M/day, dodges Groq cap |
| VLM Gemini rung | `gemini-3-flash-preview` | consider **3.5 Flash** | newest multimodal |
| VLM OpenRouter rung | `Qwen3-VL` | **STALE → replace** | no longer free on OpenRouter; use Gemma 4 31B / Nemotron VL |

## Highest-impact change

**Post-benchmark conclusion (2026-06-09): the routing cascade is already at its
free-tier optimum — make NO routing changes.** Measured facts:
- Groq `gpt-oss-20b` ~485ms = fastest free; KEEP as primary.
- The Groq cap is token/day; the existing Gemini-lite fallback is request-capped
  (~1,500 RPD ≫ our volume), so it already covers the entire post-cap day at
  714ms. The [[provider-circuit-breaker]] makes the handoff graceful.
- `gemini-3.1-flash-lite-preview` (the "newer" bump): DISQUALIFIED — 42% of calls
  spike >2s. KEEP `2.5-flash-lite`.
- Cerebras (the "cap-buster"): SKIP — slower (~1.2s) + 5 RPM + unneeded (Gemini
  already covers the cap).

The only genuinely stale item is the **VLM cascade's OpenRouter `Qwen3-VL` rung**
(no longer free → Gemma 4 31B / Nemotron VL) — but that path is SCANNER/marketplace
image evaluation, out of the voice-bot lane; hand to the scanner session.

## Caveats / do-before-shipping

- **Benchmark before swapping any routing model.** Function-calling reliability
  is what bit us (the Llama-3.3 `<function=>` parser regression). Both prior
  routing changes ([[groq-gpt-oss-20b-swap]], [[gemini-tool-router-rung]]) were
  benchmarked first — hold this bar.
- We **dropped Cerebras before** ([[drop-cerebras-from-cascade]], 2026-04-23) for
  chronic 429s — but that was `qwen-3-235b`; the free tier now serves
  `gpt-oss-120b` at 1M/day. Re-evaluate on the new model, don't assume.
- ⚠️ Unconfirmed (verifier hit the limit): Gemini 2.5/3.5 exact free RPD,
  Cerebras real-world RPM under bursty voice, Groq's precise limit shape. Resolve
  before relying on them.
