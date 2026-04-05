# Research Prompt: Custom Wake Word Detection + Response Latency for Discord Voice Bot

## Two Connected Problems

We have a Discord voice bot ("Poob") in group voice calls (2-6 users). Two critical issues make it unusable:

### Problem 1: Wake Word "Poob" Is Never Recognized
Every STT engine mishears "Poob" (not a real word):
- Groq Whisper: "Boob", "Boop", "Oob", "Hoob", "Tube", "Prairie"
- Deepgram Nova-3: "Hoop", "Oobe", "Poop" (sometimes correct)
- Gemini Flash: Hallucinates entirely ("a single mom" for "what's up Poob")

We have a wide regex catching 30+ variants but STT still misses it ~40% of the time. The audio quality from Discord is confirmed clear — we saved and listened to the WAV files.

### Problem 2: 5-10 Second Response Latency
Even when the wake word IS detected, the pipeline takes too long:
```
User finishes speaking
→ VAD detects end of speech (300-500ms)
→ STT transcription via API (1.5-3.5 seconds — THE BOTTLENECK)
→ Wake word regex check (instant)
→ LLM response generation (500ms)
→ TTS synthesis (800-1200ms)
→ Audio playback begins
Total: 4-8 seconds perceived latency
```

The STT step is the bottleneck. It processes the ENTIRE utterance as a batch upload, waits for the full transcription, THEN checks for the wake word. This is fundamentally backwards — we should detect the wake word FIRST (in real-time as audio streams in) and only do full STT if the wake word was detected.

## Current Architecture
- Audio: Discord DAVE E2EE → Pycord voice receive → per-user PCM (48kHz stereo)
- VAD: Energy-based RMS threshold + packet gap detection
- STT: Deepgram Nova-3 (primary) → Groq Whisper (fallback) — batch API
- Wake word: Regex on STT transcript text
- LLM: Groq llama-3.1-8b-instant (500ms)
- TTS: Google Chirp3-HD Fenrir (800ms)
- Python 3.13, Windows 10

## What We Need Researched

### 1. Custom Wake Word Engines (Audio-Level, Pre-STT)
These detect the wake word directly from raw audio, BEFORE any STT runs:

- **Picovoice Porcupine** — custom wake word training, runs on-device, <1ms detection
  - Can we train a custom "Poob" wake word?
  - Free tier? Pricing?
  - Python SDK? Does it accept 48kHz PCM?
  - Latency for detection?

- **OpenWakeWord** — open source, runs locally, can train custom words
  - https://github.com/dscripka/openWakeWord
  - Can it handle Discord's 48kHz stereo audio?
  - How accurate is it for custom non-English words?
  - Training process — how much data needed?

- **Snowboy** (if still maintained) or other options?

- **Whisper-based keyword spotting** — could we run a tiny local Whisper model
  ONLY on the first 500ms of each utterance to check for the wake word?
  Would be faster than full STT since it's a tiny audio clip.

### 2. Streaming STT for Faster Wake Word + Full Transcription
Instead of batch STT (upload entire utterance, wait for result), use streaming:

- **Deepgram Streaming API** — WebSocket-based, returns words as they're spoken
  - Could detect "Poob" within 200-300ms of it being said
  - Then continue streaming for the full transcription
  - Does Deepgram streaming support custom vocabulary/keywords?
  - Latency for first word vs batch API?

- **Google Cloud Speech-to-Text Streaming** — similar streaming approach
  - Does it support speech adaptation (boosting "Poob")?
  - Free tier limits?

- **AssemblyAI Real-Time** — streaming with custom vocabulary
  - Free tier?

### 3. Hybrid Architecture (Wake Word Engine + Streaming STT)
The ideal architecture both research LLMs suggested:

```
Raw audio from Discord (per-user)
    ↓ (parallel paths)
    ├→ Local wake word detector (Porcupine/OpenWakeWord, <5ms)
    │   → If "Poob" detected: flag this user's audio for full processing
    │
    └→ Streaming STT (Deepgram WebSocket)
        → Accumulates transcript in real-time
        → When wake word detector fires: transcript is already partially ready
        → Full transcription available within 200ms of wake word detection
```

This eliminates BOTH problems:
- Wake word detected from raw audio (no STT dependency)
- Full transcription already in progress by the time wake word fires
- Response starts within ~500ms of wake word, not 3-5 seconds

### 4. Training Data for Custom Wake Word
- How many audio samples of "Poob" are needed?
- Can we generate synthetic training data using TTS?
- Does the wake word engine need negative samples too?
- Can we use the debug WAV files we already saved from Discord?

### 5. Latency Optimization: Speculative Processing
Could we start the LLM BEFORE the user finishes speaking?
- Begin LLM generation as soon as the wake word is detected
- Feed partial transcription to the LLM
- Update/correct as more words arrive
- Similar to how ChatGPT Voice starts formulating before you finish

### 6. Cost and Free Tier Analysis
For daily usage (~30 min of Poob being in calls, ~5 min active processing):
- Picovoice Porcupine: free tier limits?
- Deepgram streaming: cost per minute of streaming audio?
- OpenWakeWord: completely free (local)?
- What's the most cost-effective combination?

## Constraints
- Must work with 48kHz stereo PCM from Discord (can downsample)
- Must run on Windows 10 with Python 3.13
- Must support multiple users simultaneously (per-user audio streams)
- Total budget: ~$5/month for API costs
- Wake word detection must be <100ms from when the word is spoken
- Full pipeline (wake word → response audio) must be <2 seconds

## Desired Outcome
A system where:
1. Someone says "Poob" and the bot detects it within 100ms (not 2-3 seconds)
2. The full transcription of what they said is available within 300ms of them finishing
3. The bot starts responding within 1 second of the user finishing their sentence
4. Works reliably in group calls with 2-6 users talking over each other
5. "Poob" is detected >95% of the time regardless of accent, speed, or background noise
