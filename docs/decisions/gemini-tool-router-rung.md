---
type: decision
status: active
date: 2026-06-02
tags: [brain, llm, cascade, gemini, tool-calling, rate-limit]
related: [[brain-routing-audit]] [[groq-daily-token-cap-degrades-routing]] [[groq-gpt-oss-20b-swap]] [[drop-cerebras-from-cascade]] [[music-routing-prompt-thoughtfulness]] [[vlm-cascade-operational-findings]]
---

# Add Google Gemini Flash-Lite as a tool-router rung (no daily token cap)

## Context

Groq's free `gpt-oss-20b` primary hits its **200k-tokens/DAY** cap mid-session ([[groq-daily-token-cap-degrades-routing]]), dropping all routing onto the NVIDIA `qwen3-next-80b` rung — which mishandled ~24% of music requests. The fix needs a $0, no-card rung whose free tier is **RPD-limited, not token-capped**, so it survives past Groq's daily wall.

A dispatched research report (June 2026; treated as supplement, not ground truth) ranked **Google Gemini Flash-Lite** the top such option: no card, ~1,000 RPD, 250k TPM, **no daily token cap**, native function-calling. We already hold a `google_api_key` (used for VLM voting + the deal-agent cascade), and Gemini exposes an **OpenAI-compatibility endpoint**, so it drops into the existing OpenAI-compat provider path with no new SDK.

## Verification before integration (never assume)

Both candidates were benchmarked against the **real** `MUSIC_TOOL`/`DEAL_TOOL` defs + the new routing prompt, inside the prod container, on the exact failure cases from the logs:

- **Local Ollama Qwen3 (qwen3:4b / qwen3:8b) — PERMANENTLY REJECTED for routing.** Accurate where it answered (correct on "play tiki tiki", both negatives "let's play a game"/"nice play dude", and the deal route), but **50–120 s per call** on this box (qwen3:8b timed out on every call). Operator confirmed (2026-06-02) the **homelab is CPU-only — there is no GPU** — so this latency is the hardware floor, not a fixable Ollama-passthrough issue. Local is off the table as a router rung for good; the report's "Stage-2 local floor" does not apply to this deployment. Local Qwen3 stays viable only for *latency-tolerant* tasks, never real-time routing.
- **Gemini 2.5 Flash-Lite (OpenAI-compat) — ADOPTED.** **8/8 correct** routing decisions at **584–819 ms**: "play tiki tiki" → `query:"tiki tiki"`, "play starships" → `starships` (no hallucination), "skip"/"put on phonk" correct, both negatives → no tool, wishlist → deal, casual → no tool. Reliable, fast, $0.

## Decision

1. **Cascade position:** Groq `gpt-oss-20b` → **Gemini `gemini-2.5-flash-lite`** → NVIDIA `qwen3-next-80b` → Groq Scout (last resort). Gemini sits *before* NVIDIA because it's the stronger, no-token-cap function-caller; when Groq 429s on its daily cap, Gemini carries routing instead of the weaker NVIDIA rung. The cascade only advances to Gemini on a Groq error (e.g. the daily-cap 429), so it fires as a fallback, not on every turn.
2. **Implementation:** a `gemini` branch in `_call_provider_with_tools` ([brain/poob.py](../../src/poob/brain/poob.py)) pointing at `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions` with `Authorization: Bearer {google_api_key}`, `temperature=0` for deterministic routing. Reuses the shared OpenAI-compat post+parse — no new dependency. New `PoobBrain` fields `google_api_key` + `gemini_router_model`, wired from `config.google_api_key` / `config.agent_google_model` in `main.py`.

## Quota consideration

`gemini_router_model` defaults to `gemini-2.5-flash-lite` — the same model as the deal-agent's late Gemini fallback (`agent_google_model`). Gemini free quotas are **per-project**, so they share that model's ~1,000 RPD. Acceptable because both are *fallbacks* (the router rung only fires when Groq errors; the deal-agent Gemini is a deep rung), so combined RPD stays well under 1,000 in normal use. If contention ever appears, point `gemini_router_model` at a different Gemini model (separate per-model quota) via a one-line `main.py` change. The VLM voters use different Gemini models (`gemini-3.1-flash-lite-preview`, `gemini-3-flash-preview`) and are unaffected.

## What the report confirmed (no action — already correct)

- Groq `llama-3.3-70b-versatile` `<function=...>` parser bug → matches [[groq-gpt-oss-20b-swap]] exactly. We're off it; the recovery regex stays.
- Cerebras chronic 429 + 1M/day cap → matches [[drop-cerebras-from-cascade]]. Stays out.

## Follow-ups (not done here — avoid scope creep)

- **NVIDIA NIM ~24% mishandle may be model/parser-specific** (the report's hypothesis): NVIDIA drops the tool call into `content` when the model's tool-call template isn't post-processed. With Gemini now above NVIDIA, NVIDIA is rarely the active router, so this is lower priority — but swapping `agent_nvidia_model` to a documented function-caller (e.g. a Mistral-Nemotron / Llama-3.3-70B NIM) is a candidate if telemetry still shows NVIDIA misses.
- **Local Qwen3 floor — CLOSED, not viable.** Homelab is CPU-only (operator-confirmed); 50–120 s/call is the hardware floor. No GPU to fix. Off the table permanently for routing.
- **Additional free cloud rungs (GitHub Models / Z.AI GLM-4.7-Flash / Mistral / SambaNova) — DECLINED for now, watch-triggered.** A 3rd rung only helps if BOTH Groq's daily token cap AND Gemini's ~1,000 RPD are exhausted in one day. Grounded check (persistent voice.log, ~20.5 h, 2026-06-02): **1,248 wake-word detections but only 35 actual `poob.tool_route` calls** — the vast majority of wake events are overridden/passive and never reach the router. Effective post-Groq-cap routing volume is well under Gemini's 1,000 RPD; `provider=gemini` calls so far = 0. So an extra rung would essentially never fire. **Trigger to revisit:** a real signal that Gemini's RPD is being hit — `provider=gemini` 429 / RESOURCE_EXHAUSTED in prod. At that point, provision a Z.AI (GLM-4.7-Flash, ~1,000 RPD, agentic-tool-built) or Mistral (monthly budget) key and benchmark + wire it exactly as Gemini was. Adding it speculatively now is unbenchmarked complexity for a scenario we don't reach (same principle as [[brain-routing-audit]]'s "expand only when telemetry justifies the cost").

## Validation

- `tests/unit/test_brain_gemini_router.py`: branch parses OpenAI tool_calls + hits the right endpoint with Bearer auth; Gemini sits between Groq and NVIDIA; uses `gemini_router_model`; absent without a key.
- Full unit suite green.
- **Exit criterion (post-deploy):** during a Groq daily-cap window, `poob.tool_route ... provider=gemini` appears and `Safety net caught` / hallucination-drop counts stay low even after Groq's cap is hit — routing no longer degrades to NVIDIA's miss rate.

## Rollback

Remove the `("gemini", …)` append in `_groq_with_tools` (one block) — or unset `google_api_key` on the brain. Cascade reverts to Groq → NVIDIA → Scout.
