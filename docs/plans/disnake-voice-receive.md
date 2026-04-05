# Plan: Custom Voice Receive on Disnake + dave.py (libdave C++)

## Why This Is The Only Path

Every Python library using davey (OpenMLS) for DAVE has the same multi-user
epoch desync bug. It's confirmed across discord.py, discord.js, and JDA.
The ONLY DAVE implementation that handles multi-user correctly is Discord's
own C++ libdave (used by dave.py). disnake uses dave.py but lacks voice receive.

**Solution: Build custom voice receive on disnake's infrastructure.**

## Architecture

```
disnake VoiceClient (DAVE via dave.py/libdave ✓)
    ↓
Raw UDP socket (already established by disnake)
    ↓
Custom PacketListener thread (NEW — we build this)
    → Read UDP packets from disnake's socket
    → Parse RTP headers (extract SSRC, sequence, timestamp)
    → Transport decrypt via nacl.secret.Aead (we have the session key)
    → DAVE frame decrypt via disnake's DaveState.decrypt()
    → Opus decode via discord.opus.Decoder
    → Feed PCM to our existing VAD → STT → LLM → TTS pipeline
```

## What disnake Already Provides
- VoiceClient.connect() — joins channel with DAVE handshake via dave.py
- VoiceClient.socket — the raw UDP socket (accessible)
- VoiceClient.ssrc — bot's own SSRC
- VoiceClient.secret_key — transport encryption key (32 bytes)
- VoiceClient.mode — 'aead_xchacha20_poly1305_rtpsize'
- VoiceClient.dave — DaveState object with can_encrypt(), encrypt(), decrypt()
- VoiceClient.endpoint_ip, voice_port — UDP endpoint

## What We Need To Build
1. **PacketListener** — Thread that reads UDP packets from disnake's socket
2. **RTPParser** — Extract SSRC, sequence, timestamp from RTP header
3. **TransportDecryptor** — AEAD XChaCha20-Poly1305 using session key
4. **DAVEDecryptor** — Use disnake's DaveState to DAVE-decrypt the payload
5. **OpusDecoder** — Decode opus to PCM (one decoder per SSRC/user)
6. **SSRCMapper** — Map SSRC → Discord user ID (from Speaking events)
7. **Integration** — Feed decoded PCM into our existing VoiceSession pipeline

## Key Insight: We Don't Need discord-ext-voice-recv
That library monkey-patches discord.py's VoiceClient internals. We're building
from scratch on disnake's clean socket, which means no compatibility issues.

## Dual-Framework Architecture
- **disnake** handles: voice connection, DAVE, UDP socket, audio playback
- **discord.py** handles: text commands, cogs, patrol engine, all non-voice
- Both share the same bot token
- Only ONE gateway connection (discord.py's) — disnake connects voice only
- disnake doesn't need a full Client — just VoiceClient functionality

## Implementation Steps
1. Install disnake (already done)
2. Build PacketListener + RTPParser + TransportDecryptor
3. Wire DAVE decrypt through disnake's DaveState
4. Build per-user OpusDecoder pool
5. Connect to existing VoiceSession (VAD → STT → LLM → TTS → playback)
6. Replace discord.py voice connection with disnake voice connection
7. Keep discord.py for everything else (text, patrol, cogs)
