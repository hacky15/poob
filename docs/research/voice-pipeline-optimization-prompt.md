---
type: research
status: resolved
date: 2026-04-23
tags: [voice, tts, stt, llm, latency, research-prompt]
superseded_by: [[voice-bot-latency-april2026-findings]]
related: [[voice-bot-latency-april2026-findings]] [[voice-architecture]] [[voice-latency-optimization]] [[wake-word-latency]] [[conversation-classifier]] [[poobbrain-architecture]]
---

# Research prompt: voice pipeline latency + accuracy (April 2026)

The prompt below is written to be dropped into a research LLM (Perplexity Pro, Claude with web, Gemini Deep Research, etc.) cold. It contains the entire stack description, the numbers we're seeing today, and — critically — every approach we've already tried and rejected so the researcher doesn't point us back at dead ends.

Current state is **the best we've had**, so recommendations must credibly improve on it without regressing the parts that finally work.

---

## Prompt to send

You are a senior voice-AI engineer. I'm running a Discord voice bot called Poob in real multi-user voice channels (2–6 people, often with game audio underneath). I want concrete, current recommendations — **April 2026 timeframe** — for reducing latency and improving accuracy across our **speech → intent → speech** pipeline, WITHOUT regressing what already works.

Please respond with specific models, libraries, or architectural patterns, version numbers where relevant, and tradeoffs (latency vs accuracy vs cost vs self-host-feasibility). **Include released-in-2026** options — don't just recommend well-known 2024-vintage tools. Skim your knowledge for recent releases and use web search if available.

## Our current stack (every layer)

**Platform:** Python 3.11 container on a homelab (Docker + Komodo + GHCR auto-deploy), limited consumer GPU (single 4090 class). Commercial-scale inference budget is zero; we run on free tiers and self-hosted models. Multi-user Discord voice channels, DAVE-encrypted audio.

**Voice transport:** Pycord 2.7.0 with `voice_compat` DAVE patch (adapted from GabrielAgrela/Discord-Brain-Rot). Custom Pycord `Sink` captures per-user PCM in real time. This layer is stable and load-bearing — **do not suggest discord.py, Lavalink, or disnake**; we've been there and come back.

**Wake-word:** openwakeword with a *custom-trained* `hey_poob.onnx` model. Local, ~50ms inference, threshold 0.7. Keeps firing false positives in noisy game-audio backgrounds and missing clean wake words when music is playing under the speaker.

**Streaming STT:** Deepgram Nova-3 as primary, via streaming websocket. Keyterms biased toward music control words (`play`, `skip`, `stop`, `pause`, `volume`, `shuffle`). Two local fallbacks: Gemini 2.5 Flash Lite STT and Groq Whisper Large v3 Turbo.

**Dual-gate address detection** (what we built after failed experiments):
- Text-regex match on transcript AND acoustic wake-word signal → addressed.
- Text-only match while bot is silent → addressed (acoustic model misses in noise).
- Text-only match while bot is TTS-speaking → rejected (mic loopback of bot's own voice hallucinates wake).
- `bot_audio_active()` returns true **only** when bot is speaking (TTS in progress). Music playing alone does not gate wake detection because music lyrics don't contain wake phonemes.

**Brain / intent routing:** PoobBrain (thin personality + tool-call router). Tool-calling cascade:
1. Groq `llama-3.3-70b-versatile` — **currently 400-ing on every tool call** with a validation error we haven't captured the full text of yet. Wastes ~500ms.
2. Cerebras `qwen-3-235b-a22b-instruct-2507` — **currently 429-rate-limited** in production.
3. NVIDIA NIM `qwen3-next-80b-a3b-instruct` — usually succeeds, ~1.2s first-token.
4. Groq `llama-4-scout-17b` — last resort, tool-hallucinates ("Did you get offended?" → plays "Big Ole Freak").

Casual responses (no tool call) hit `llama-3.3-70b-versatile` too, average ~200-400ms first token.

**Music wrap ("Toob"):** Separate LLM call after music tool action, `llama-3.1-8b-instant` for a ≤12-word dramatic reaction. Adds ~0.5-1.5s serial latency after music search completes.

**TTS — Poob voice:** Google Chirp3-HD-Fenrir primary; Edge TTS RogerNeural fallback. ~0.5-1s synth.

**TTS — Toob voice (music):** Google Chirp3-HD-Enceladus at 0.95x speaking rate, then FFmpeg warlord chain: `asetrate=18500, aresample=24000, atempo=2.0, vibrato=f=5.5:d=0.15, bass=g=6:f=80, aecho=0.8:0.85:40:0.3, volume=1.35`. Filter chain adds ~100-230ms (cold start ~770ms).

**Loudness normalization:** FFmpeg `speechnorm=e=12.5:r=0.0001:l=1` at decode step. Single-pass, ~zero latency.

**Audio playback:** Pycord `FFmpegPCMAudio` → `PCMVolumeTransformer(volume=3.0)` → custom `MixingAudioSource`. When music is playing, TTS is injected as an overlay and music ducks to 25% with 15-frame (300ms) gain ramps. Mixing uses `audioop` (C extension), int16 clipping baked in.

**Music playback:** yt-dlp pre-download to disk (eliminates YouTube CDN TLS termination), `BufferedAudioSource` (2-second queue absorbs I/O jitter), FFmpegPCMAudio.

## Latency budget — what we're seeing in prod

For a music play request, end-of-user-speech to Poob-starts-speaking:
- ~500ms: Groq tool-call 400 retry
- ~200ms: Cerebras 429 retry
- ~1200ms: NVIDIA first-token
- ~3700ms: **music action (ytdl search/resolve) + Toob wrap LLM call** — the dominant sink
- ~1100ms: Toob TTS synth + FFmpeg filter chain
- **Total: ~6.7 seconds before the first word comes out.** Then Toob speaks for another 3s.

For a casual voice reply:
- ~2-3 seconds before the first word.
- Then Poob speaks for 5-13 seconds depending on reply length.
- Casual replies were recently tightened to "1-2 sentences, ~20 words" — helped, not enough.

## What we've tried and explicitly rejected

**Do not suggest any of these unless there's a 2026 development that changes the calculus.**

- **Silero VAD** — we own the code (`voice/silero_vad.py`), it's disabled. Per-user model instances are too slow to initialize in multi-user channels. Would need a shared-instance architecture with per-user state.
- **Kokoro TTS** — infrastructure built, disabled. Latency and voice quality didn't justify replacing Google/Edge in our tests.
- **`loudnorm`** — too slow (two-pass, ~100-200ms). `speechnorm` replaced it.
- **numpy for audio mixing** — replaced with `audioop` (C ext). ~5-10× faster, zero allocations per frame. Do not regress.
- **Lavalink for music** — JVM overhead (300-500MB), no native PCM mixing, loses our TTS overlay capability.
- **Mocking databases in tests** — prod divergence bit us; integration tests hit real SQLite in-memory instead.
- **`_TOOL_INTENTS` keyword-routing fallback** — fragile English-only bandaid. Deleted.
- **OG tags / JSON-LD for Facebook** — doesn't exist for browser UAs. Dead path.
- **Acoustic-only wake word** — misses clean wakes in noise.
- **Text-only wake word** — hallucinates from ambient audio / music lyrics.
- **"Hard dual-gate" (both required always)** — rejected legitimate wakes when openwakeword missed in noise.
- **Filler audio during LLM wait** — built in `voice/fillers.py`, not yet triggered. Open question: which LLM latency windows justify a filler vs being annoying.
- **Scout 17B as primary tool-caller** — aggressively over-routes to music_assistant, plays random songs on unrelated prompts.
- **8b-instant Groq for tool calling** — fails with 400 on short messages.

## Specific questions

**1. End-to-end realtime voice models (highest priority).**
- Are OpenAI Realtime API, Google Gemini Live, Groq realtime endpoints, or any new-2026 realtime models mature enough to replace our STT + brain + TTS triple serially? What latency do they actually deliver for multi-user server-side inference in April 2026?
- Specifically: can any of them (a) ingest per-user audio streams with speaker identity preserved, (b) run local wake-gating before sending audio upstream to control cost, and (c) produce streamable speech output we can re-inject into the Discord voice client?
- What free or low-cost free tiers exist in April 2026 that haven't hit the same 429 wall Groq/Cerebras did?

**2. Streaming TTS.**
- Which production-grade TTS models in 2026 support **true sentence-streaming** (first audio chunk within ~200ms of text input)? We want to start playing audio before the LLM finishes generating.
- We already stream LLM output sentence-by-sentence and synth concurrently. What we want: a TTS model where the synth *itself* streams chunk-by-chunk within a single sentence, so first audio appears within ~100ms.
- Candidates to evaluate: StyleTTS 2 family, latest Eleven Labs streaming mode, Kokoro 82M/v2, Fish Speech / Fish Audio, XTTS v2 + v3, OpenVoice V2, Sesame CSM, Piper, whatever's new in 2026.

**3. Wake-word model replacement.**
- Openwakeword misses in noisy environments. What 2025-2026 small-footprint wake models handle game-audio backgrounds better while staying <50ms on CPU?
- Custom-training a wake word for "Hey Poob" took effort; we want to keep that capability. What training pipelines look best in 2026 for a custom wake word?
- Candidates: Porcupine, Sensory TrulyHandsfree, micro-wake-word, Whisper-based pseudo-wake, custom encoder on top of a small SSL model (WavLM tiny, HuBERT tiny).

**4. STT alternatives.**
- Deepgram Nova-3 streaming is our baseline. What alternatives in April 2026 are **faster first-word** (not just total latency) and handle keyterm biasing comparably? Specifically for short commands with music/gaming jargon.
- Candidates: Distil-Whisper v3+, Moonshine, AssemblyAI Realtime, Speechmatics, open-source Whisper v4 if it exists, on-device options for GPU-accelerated streaming STT.
- How much of a win is true on-device STT vs cloud streaming for our use case? (WAN latency to Deepgram is ~100-200ms that we can cut to ~0.)

**5. Tool-calling LLM options.**
- Our Groq 70b is 400-ing and Cerebras is 429-ing. What **new-2026 small tool-calling LLMs** (3-13B) would have comparable routing quality at lower-latency and fewer rate-limit gates? Self-hosting options welcome.
- Specifically: what's the smallest model that reliably emits correctly-formatted OpenAI-style `tool_calls` JSON for a 3-tool schema (deal_assistant, music_assistant, and casual-response) WITHOUT hallucinating tool calls from passive conversation context?
- Candidates: Phi-4 family, new Qwen3/4 small tool variants, Llama 4 small, Mistral Codestral-small, xLAM.

**6. Architectural patterns.**
- We serialize (tool-routing LLM) → (music search via yt-dlp) → (Toob wrap LLM) → (TTS synth) → (playback). The Toob wrap blocks on music search completion. **Any pattern to speculatively begin the wrap before search completes**, or to pipeline music search + wrap + TTS concurrently without regressing correctness?
- Does a single multimodal audio-in / audio-out LLM (like Gemini Live or an equivalent) collapse these four stages into one round-trip cheaply enough to matter for us?
- What do other Discord voice-AI projects doing similar work look like in 2026? Any public codebases with lower latency we should study?

**7. Groq 400 root cause — bonus question.**
- Our `llama-3.3-70b-versatile` call with `tool_choice=auto` and 3-tool schema is 400-ing with `"Failed to call a function"` / `"tool call validation"`. Full error is truncated in our logs. **What causes Groq's tool_call validation to 400 in April 2026?** Has their tool-call schema contract tightened? Is there a known breaking change? If you know, tell us what to check.

## Deliverable format

Please produce:

1. **Top 3 highest-leverage changes** with expected latency-improvement numbers and concrete implementation effort. Rank-ordered.
2. A **realistic end-to-end latency projection** if we adopted your top recommendations.
3. A **"don't bother" list** — things that sound good but wouldn't actually help us, and why.
4. Links to current documentation, benchmark pages, or github repos for every recommendation.

Skip generic advice ("use a faster model") and background explanations of what STT/TTS/LLM are. Assume I know the domain. Where numbers exist, cite them.

---

## How to dispatch this

- Paste into Perplexity Pro with "Deep Research" mode.
- Or Claude with web search enabled.
- Or Gemini 2.5 Deep Research.
- Or spawn a research sub-agent with web access (WebSearch + WebFetch tools).

Save returned findings as a new note in `docs/research/` with `type: research, status: active`. Any concrete change recommendations should open corresponding decision notes in `docs/decisions/`.
