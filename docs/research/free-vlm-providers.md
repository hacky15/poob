# Research: Free-Tier VLM/LLM Providers for Deal Evaluation
**Date:** March 17, 2026 | **Status:** CURRENT | **Sources:** results_3.md
**MUTABLE: Update when provider limits change or new providers appear.**

## Google AI Studio (Current Primary)

### Actual Limits (post-December 2025 cuts)
| Model | RPM | RPD | TPM | Notes |
|-------|-----|-----|-----|-------|
| Gemini 2.5 Flash | 10 | 250 | 250K | Best quality, most restricted |
| Gemini 2.5 Flash-Lite | 15 | 1,000 | 250K | Highest throughput |
| Gemini 2.5 Pro | 5 | 100 | 250K | Tiebreaker only |
| Gemini 3 Flash Preview | lower | lower | 250K | Preview, more restricted |
| Gemma 3 27B | varies | varies | varies | Free only (no paid tier) |

- Quotas are **per-model independent** (using Flash doesn't consume Flash-Lite quota)
- Combined theoretical: 1,350 RPD across all models
- RPD resets at **midnight Pacific Time**
- **UNDOCUMENTED: Images Per Minute (IPM) limit** — 2-10 images/minute for free tier
  - This is why we exhaust after ~8 evaluations
  - Burst of images triggers IPM circuit breaker even if RPM is fine
  - Solution: leaky bucket to smooth image submission rate

### Gemini 2.0 Flash: DEPRECATED
- Shutdown June 1, 2026 — must migrate to 2.5/3.x

## Free VLM Providers (Ranked by Quality)

### Tier 1: Primary Voters

| Provider | Model | RPM | RPD/Daily | Quality | Integration |
|----------|-------|-----|-----------|---------|-------------|
| Google AI Studio | Gemini 2.5 Flash | 10 | 250 | Highest | google-genai SDK |
| Groq | Llama 4 Scout 17B | 30 | 14,400 | High | OpenAI-compatible |
| Together.ai | Llama-Vision-Free (11B) | ~60 dynamic | Unlimited* | Good | OpenAI-compatible |
| Mistral | Pixtral 12B | 60 (1 RPS) | Unstated | Good | OpenAI-compatible |

*Together.ai: dynamic rate limiting — steady pace works, bursts get throttled

### Tier 2: Secondary/Fallback

| Provider | Model | RPM | RPD/Daily | Quality | Integration |
|----------|-------|-----|-----------|---------|-------------|
| Google AI Studio | Gemini 2.5 Flash-Lite | 15 | 1,000 | Good | google-genai SDK |
| Cloudflare Workers AI | Llama 4 Scout 17B | varies | 10K neurons/day | Good | REST API |
| Fireworks.ai | Qwen3 VL 30B | 60 | $1 credit pool | High | OpenAI-compatible |
| NVIDIA NIM | Llama 3.2 11B/90B | 40 | varies | Good | OpenAI-compatible |

### Tier 3: Emergency

| Provider | Model | Limit | Notes |
|----------|-------|-------|-------|
| Google AI Studio | Gemma 3 27B | 1,000 RPD | Free only, no paid tier |
| OpenRouter | Mistral Small 3.1 free | Variable | Often 429s |
| OpenRouter | Nemotron Nano 12B VL free | Variable | Unreliable |
| Cohere | Aya Vision 32B | 1,000/month total | Tight but usable as 3rd voter |
| HuggingFace | Qwen2.5-VL | $0.10/month | Volatile, emergency only |

### DISQUALIFIED
- **SambaNova**: 20 RPD — mathematically insufficient
- **GitHub Models**: Token limits too restrictive on free Copilot plan

## Local VLMs (Under 4GB VRAM)

| Model | VRAM | Speed | Best For |
|-------|------|-------|----------|
| Gemma 3 4B (int4 QAT) | ~2.6GB | Fast | Primary local — highest quality at size |
| Moondream2 2B (4-bit) | ~2.45GB | 184 tok/s | Built-in caption/query/detect APIs |
| SmolVLM2 2.2B | ~2GB FP16 | Fast | 64 visual tokens per patch (memory efficient) |

## Text-Only LLMs

| Provider | Model | RPM | TPM | TPD | Quality | Notes |
|----------|-------|-----|-----|-----|---------|-------|
| Cerebras | Qwen 3 235B | 30 | 60K | 1M | Excellent | No vision. JSON structured output reliable with `strict: true` |
| Groq | Llama 3.3 70B | 30 | varies | 14,400 RPD | Good | Our current triage model |

### Cerebras JSON Reliability
- Use `response_format.type = "json_schema"` with `strict: true`
- Activates constrained decoding (logits masking) — guarantees valid JSON
- The "thinking" tag contamination issue (`<think>`) is resolved with strict mode
- 60K TPM is the binding constraint — hit within 10-20s of sustained use

## Rate Limit Strategy (from research)

### Leaky Bucket > Token Bucket
- Free-tier APIs penalize burst traffic (Google's IPM, Together's dynamic throttling)
- Leaky bucket guarantees smooth, constant outbound rate — prevents triggering anomaly detection
- Token bucket ALLOWS bursts — triggers Google's IPM circuit breaker

### Preemptive Throttling at 85%
- Drop provider priority BEFORE hitting limits
- Eliminates retry overhead and exponential backoff cascading

### Per-Cycle Budget Allocation
```
budget_per_cycle = provider_rpd * 0.80 / expected_daily_cycles
```
- 20% reserve for retries
- Prevents early cycles from starving later ones

### Graceful Degradation
- Accept 2-of-3 votes when providers exhausted
- Queue unevaluated images for next cycle
- Never block the pipeline waiting for a single provider
