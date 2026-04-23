---
type: research
status: active
date: 2026-03-17
tags: [voice, address-detection, stt, llm]
related: [[voice-architecture]] [[wake-word-dual-gate]]
---

# Research Prompt: Intelligent Conversation Address Detection for Multi-User Voice Bot

## The Problem

We have a Discord voice bot ("Poob") that sits in voice channels with 2-6 users having a live conversation. Poob passively transcribes ALL audio (via Groq Whisper STT) into an attributed rolling transcript. When someone addresses Poob, it responds using an LLM with full conversation context.

**The core challenge:** How does Poob know when it's being talked to vs. when people are just talking to each other?

## What We've Built

### Current Architecture
- **Audio pipeline:** Discord DAVE E2EE → Pycord voice receive → per-user VAD → Groq Whisper STT → rolling transcript
- **Transcript format:** Each entry is attributed: `Ben (hacky15): yeah thats what I was saying`
- **Address detection:** Currently wake-word only ("Poob", "Poop", "Poo" etc.)
- **Response pipeline:** LLM (Groq, 600-800ms) → Google Chirp3-HD TTS → Discord voice output
- **Context window:** Last 25 messages from all users, attributed with names

### What Works
- Wake word detection (regex match on transcript) — zero false positives
- Passive context accumulation — Poob knows what people discussed even when silent
- When addressed, Poob responds naturally using full context
- Response latency: ~3-4 seconds (utterance end → first audio out)

### What We Tried and Failed

#### 1. Reply Pattern Matching (regex)
Patterns like `^(but |what about |you |your |really |dude |bro )` after Poob recently spoke.
- **Result:** Too many false positives. "But what about dinner?" said to a friend matches the pattern because it starts with "but". In a group call, EVERYONE uses these words within 30 seconds.

#### 2. LLM Classifier (Groq llama-3.1-8b-instant, ~200-400ms)
Prompt: "Is this message directed at Poob? yes/no" with recent context.
- **Result:** Even worse false positives. The LLM treats any mention of AI, bots, or topically relevant content as "directed at Poob." Example: User says "You gotta be nice to AI creatures" to another user → classifier says YES because it's about AI. User says "you know," mid-sentence to a friend → classifier says YES because Poob recently spoke.
- **Root cause:** The LLM can't reliably distinguish "talking ABOUT Poob" from "talking TO Poob" in a noisy multi-party conversation. It's especially bad when Poob recently spoke, because then everything seems like a reply.

#### 3. Conversation Window Timer (45 seconds)
After Poob speaks, stay "active" for 45 seconds and use reply patterns + LLM classifier.
- **Result:** In a group call with 4-6 people, someone ALWAYS talks within 45 seconds. The window never closes. Effectively makes Poob respond to everything.

## What Flagship Products Do

### Amazon Alexa / Google Home / Siri
- **Wake word only.** No inference about who's being addressed.
- Works because they're appliances, not conversational participants.
- **Not what we want.** We want Poob to feel like a person in the call, not an appliance.

### ChatGPT Voice / Gemini Live / Grok Voice
- These are 1-on-1 conversations. There's no multi-party problem — everything said is directed at the AI.
- **Not applicable** to our multi-user Discord scenario.

### Google Duplex (restaurant reservation bot)
- Also 1-on-1. The human on the other end is always talking to the bot.
- Uses turn-taking models but doesn't need address detection.

### Discord Clyde (discontinued)
- Text-based, not voice.
- Used @mention as the address signal (equivalent to our wake word).

## What We Need Researched

### 1. Academic Research on Multi-Party Addressee Detection
- Are there published papers on detecting who is being addressed in multi-party conversations?
- What features matter most? (gaze direction, prosody, semantic content, conversation structure)
- In audio-only (no video/gaze), what signals are available?
- Key search terms: "addressee detection multi-party dialogue", "turn-taking prediction", "speaker-addressee recognition", "conversational floor management"

### 2. Prosody-Based Detection
- When someone talks to a bot/assistant vs. to a person, does their voice change?
- Is there research on detecting "addressed to assistant" vs "addressed to human" from audio features alone (pitch, speaking rate, formality)?
- Could we extract prosody features from the raw audio BEFORE STT and use them as a signal?
- Example: "Hey Poob what do you think" likely has a different intonation pattern than "dude what do you think" said to a friend.

### 3. Contextual Turn-Taking Models
- Are there models that predict "whose turn is it to speak" in multi-party conversation?
- Could we frame this as: "Given the conversation flow, would a human participant named Poob reasonably believe they're being addressed?"
- Research on "end-of-turn prediction" and "next-speaker prediction" in dialogue systems.

### 4. Hybrid Wake Word + Smart Activation
- Is there a middle ground between "wake word only" and "always listening"?
- Example: Wake word activates Poob, then Poob stays in the conversation for N exchanges (not N seconds). After N exchanges where nobody addresses Poob, it goes back to passive.
- How do smart speakers handle follow-up commands? ("Hey Alexa, play music" → "make it louder" without re-saying "Alexa")
- Google's "Continued Conversation" feature — how does it decide when the conversation is over?

### 5. The "You" Problem
- In English, "you" is ambiguous — it could be addressing anyone.
- "What do you think?" could be directed at Poob or at another person.
- How do dialogue systems handle this? Is there research on resolving "you" in multi-party settings?
- Possible signal: If Poob was the last speaker, "you" likely refers to Poob. If another person was the last speaker, "you" likely refers to them.

### 6. Lightweight On-Device Models
- Are there small, fast models (<100ms inference) specifically trained for addressee detection?
- Could we fine-tune a tiny classifier on our specific scenario (Discord voice call, 2-6 users, bot named Poob)?
- What training data would we need? Could we generate synthetic training data?

### 7. Production Voice Bots in Multi-User Scenarios
- Are there ANY production voice bots that handle multi-user voice channels with intelligent address detection (not just wake word)?
- How do gaming voice bots (if any exist) handle this?
- How do conference call AI assistants (Otter.ai, Fireflies, etc.) handle being addressed vs. just transcribing?
- VTuber AI characters that sit in Discord calls — how do they decide when to speak?

### 8. Exchange-Based Conversation Window
- Instead of a TIME-based window (45 seconds), use an EXCHANGE-based window.
- After Poob speaks, count exchanges (back-and-forth turns between OTHER users).
- If 3+ exchanges happen between other users without addressing Poob, assume the conversation has moved on.
- Is there research supporting this approach?
- How many exchanges is the right threshold?

### 9. Semantic Relevance Scoring
- Instead of binary "is this directed at Poob?", score how relevant the utterance is to Poob's last response.
- "But what about the price?" (after Poob mentioned a deal) → high relevance
- "But what about dinner tonight?" (after Poob mentioned a deal) → low relevance
- Could we use embedding similarity between the utterance and Poob's last response as a signal?
- Fast embedding models that could score this in <50ms?

### 10. Multi-Signal Fusion
- The best approach is probably combining multiple weak signals:
  - Wake word present? (strongest signal)
  - Was Poob the last speaker? (medium signal)
  - Does the utterance semantically relate to what Poob just said? (medium signal)
  - Is the utterance phrased as a question or command? (weak signal)
  - Has anyone else been addressed by name in this utterance? (negative signal — "Simon, what do you think?" = NOT Poob)
  - How many exchanges since Poob last spoke? (decay signal)
- Is there research on fusing these signals into a reliable classifier?
- What weights/thresholds work best?

## Constraints
- **Latency budget:** <300ms for address detection. We can't add seconds of delay.
- **Audio-only:** No video, no gaze detection. Only have: transcribed text, speaker identity, conversation history, raw audio PCM.
- **Free/cheap:** No paid API calls per-utterance for classification. Local inference or very cheap cloud (Groq free tier).
- **Python 3.13 + Windows 10** environment.
- **2-6 concurrent users** in voice channel.

## Desired Outcome
A system where Poob feels like a natural participant in a group call — responding when talked to, staying quiet when people are talking to each other, and occasionally jumping in when the conversation is clearly about something Poob would know about (like deals). NOT an appliance that requires a wake word every time, but also NOT a bot that responds to every sentence spoken.

## Search Queries to Try
- "addressee detection multi-party dialogue" site:arxiv.org
- "who is being addressed in group conversation" NLP
- "multi-party turn-taking prediction model"
- "conversational AI multi-user voice" address detection
- "speaker addressee resolution audio only"
- "continued conversation smart speaker" how it works
- "Discord voice bot multi-user" address detection
- "VTuber AI discord voice call" when to speak
- "conference call AI assistant" when addressed vs transcribing
- "lightweight addressee classifier" dialogue systems
- "end of conversation detection" multi-party
- "you resolution multi-party dialogue" pragmatics
- "google continued conversation" implementation details
- "alexa follow up mode" technical implementation
- fine-tuning "addressee detection" small model
- "semantic similarity turn-taking" conversation
