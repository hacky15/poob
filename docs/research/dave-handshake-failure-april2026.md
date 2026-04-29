---
type: research
status: active
date: 2026-04-29
tags: [voice, dave, pycord, mls, voice-compat, blocking]
related: [[voice-architecture]] [[dave-ready-flag-is-not-truth]] [[dave-timeout-fail-hard-regression]]
---

# DAVE handshake failure — Poob joins but can't hear anyone (April 2026)

## Question

Why does `dave_session.ready` stay `False` for the entire call duration on the current homelab deploy, leaving Poob unable to decode any incoming user voice (Opus decoder rejects every encrypted frame as "corrupted stream", zero transcripts captured)?

## Read

- `src/poob/voice/voice_compat.py` end-to-end (~580 lines).
- `src/poob/voice/dave_patch.py` (~312 lines, marked deprecated/historical).
- Recent prod logs from `5318fc8` showing the symptom.
- Vault notes [[voice-architecture]], MEMORY's `project_pycord_migration.md`.

## What the patch does

`apply_voice_compat_patches()` in `voice_compat.py` monkeypatches `DiscordVoiceWebSocket` and `VoiceClient` so Pycord 2.7.0 can speak Discord's DAVE voice protocol. Key flow on a fresh voice connect:

1. `_patched_identify` sends IDENTIFY with `max_dave_protocol_version` from env (`VOICE_MAX_DAVE_PROTOCOL_VERSION`) or `davey.DAVE_PROTOCOL_VERSION`.
2. `SESSION_DESCRIPTION` arrives → we set `dave_protocol_version` from the gateway's offer and call `state.reinit_dave_session()`.
3. `_voiceclient_reinit_dave_session` creates a `davey.DaveSession` and sends our `MLS_KEY_PACKAGE` to the gateway.
4. The gateway is supposed to send back binary MLS frames (`MLS_WELCOME` or `MLS_ANNOUNCE_COMMIT_TRANSITION`) bundling us into the channel's MLS group.
5. `_patched_received_binary_message` processes those frames; on success the underlying `davey.DaveSession.ready` flips to True.
6. `can_encrypt` (= `dave_session.ready`) becomes True; subsequent `_patched_unpack_audio` calls run `session.decrypt(...)` on incoming opus and feed clean PCM to the decoder.

## What's happening in production

In every `/join` since the latest deploys:
- IDENTIFY succeeds.
- Voice secret key arrives (`[VoiceCompat] Received voice secret key`).
- `dave_protocol_version=1` per logs — the channel HAS DAVE.
- `dave_session.ready` never flips to True within 15s (or longer — observed across multiple rejoins).
- `can_encrypt` therefore stays False.
- `_patched_unpack_audio` skips the DAVE-decrypt branch and feeds **encrypted bytes** directly to `self.decoder.decode(frame)`.
- Decoder fails for every frame: `discord.opus "corrupted stream"`. Decoded PCM never reaches our sink. Deepgram, openwakeword, VAD all see silence.

So the bot is technically connected but functionally deaf.

## Why the welcome may not be arriving

Three plausible mechanisms — one or several may be active:

1. **Empty-channel join.** When Poob auto-joins ahead of any human, there's no MLS group yet for him to be welcomed into. His key package sits at the gateway. When humans arrive later their clients should send `MLS_PROPOSALS` to add him; the gateway should forward and we'd process them. This is the path log inspection should show, but we don't see those frames at info level.

2. **Discord protocol shift.** DAVE protocol version > 1 may now be in play in some channels. The patch was written against version 1. If Discord sends us a version-2 transition we don't understand, we wouldn't reinit correctly.

3. **Stale MLS state from prior session.** Resumes / reconnects may carry forward a `dave_session` that was never reset. `_voiceclient_reinit_dave_session` calls `session.reinit(...)` if one exists, but the gateway may not re-bundle us if it thinks our previous identity is still in the group.

We can't tell which without DEBUG-level logs from `poob.voice.voice_compat`. Currently those `logger.debug(...)` calls inside `_patched_send_binary` and `_patched_received_message` are below threshold.

## Workarounds, ranked by safety

### A. Disable DAVE entirely (config-only, no code change)

`apply_voice_compat_patches()` reads `VOICE_MAX_DAVE_PROTOCOL_VERSION` from env. Setting it to `0` makes IDENTIFY advertise `max_dave_protocol_version=0`. Discord then either:

- Sends us audio unencrypted (most likely outcome — DAVE is opportunistic and downgrades gracefully when one participant doesn't support it).
- Uses transport encryption only (`aead_xchacha20_poly1305_rtpsize`, which Pycord already handles via the secret key).

Either way, `dave_session` stays None, the unpack-audio path skips DAVE entirely, and frames feed the decoder as plain opus — which decodes correctly. **This is the lowest-risk fix.** The only cost is loss of channel E2E encryption while Poob is in the call. For our use case (bot listening for STT, broadcasting TTS) this is acceptable.

To apply: in the Komodo stack env for `poob`, set:
```
VOICE_MAX_DAVE_PROTOCOL_VERSION=0
```
Redeploy. Logs should show `max_dave=0` in the patch banner instead of `max_dave=1`. Wake words and STT should resume immediately.

### B. Crank DEBUG logging on `poob.voice.voice_compat` and re-investigate

Set `LOG_LEVEL_VOICE_COMPAT=DEBUG` (or whatever the project's logging config supports) so the binary-frame send/receive lines surface. Then we'd see exactly which MLS frames flow after the key-package send. With those logs, the actual mechanism (1, 2, or 3 above) becomes provable. Then a targeted fix can land.

Higher effort, but produces the real answer rather than a workaround.

### C. Update `voice_compat` against a newer DAVE-supporting reference implementation

The patch was adapted from `GabrielAgrela/Discord-Brain-Rot` per MEMORY's `project_pycord_migration.md`. If that project has a newer version, diffing against ours might surface a missed opcode / handler. Long-term hygiene; not a same-day fix.

## Recommended next step

**Option A (env flag) immediately.** Costs nothing, restores function. While running on Option A, we can investigate the real DAVE mechanism on a separate branch with debug logging — no production pressure.

If A produces "channel requires DAVE" rejection (unlikely but possible if your specific Discord guild has DAVE *required*), fall through to B with DEBUG logs and gather evidence before changing any voice_compat code.

## Vault discipline note

Per CLAUDE.md's mandatory vault-first rule, I am NOT changing code in this turn. The `bb51d8e` revert restored the prior soft-fail behavior; further changes to voice_compat or the handshake-wait loop must cite this research note (or a follow-up) showing which of mechanisms 1/2/3 is actually firing. Speculative DAVE patches caused the previous regression; we don't repeat that.

## Open questions for follow-up

1. Are MLS binary frames being received at all after our `MLS_KEY_PACKAGE` send? (Need DEBUG logs to answer.)
2. Does the channel actually require DAVE, or is it opportunistic? (Try `VOICE_MAX_DAVE_PROTOCOL_VERSION=0` to find out.)
3. Are there other Pycord bots in this channel known to be working? (Comparison would isolate environmental vs code factors.)
