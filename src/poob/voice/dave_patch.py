"""Runtime patch for DAVE E2EE voice in discord.py + discord-ext-voice-recv.

Addresses TWO critical issues:

1. **Receive**: discord-ext-voice-recv doesn't decrypt DAVE inner layer.
   We monkey-patch PacketDecoder._process_packet to call dave_session.decrypt()
   before opus decode.  (PR #54 on voice-recv, pending merge.)

2. **Send**: discord.py's AudioPlayer thread silently crashes when
   encrypt_opus() throws an exception (race condition with concurrent
   decrypt() calls from voice_recv thread, or uninitialized codec state).
   We monkey-patch VoiceClient._get_voice_packet to catch NIF exceptions
   and add a threading lock to serialize all dave_session access.

References:
  - https://github.com/imayhaveborkedit/discord-ext-voice-recv/pull/54
  - https://daveprotocol.com/
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

_patched = False

# Global lock serializing all access to dave_session (encrypt + decrypt).
# The AudioPlayer thread calls encrypt_opus() while voice_recv's PacketRouter
# thread calls decrypt() — without a lock, concurrent access to the MLS
# ratcheting tree causes Rust panics across the FFI boundary.
dave_lock = threading.Lock()


def apply_dave_patch() -> bool:
    """Monkey-patch discord.py and voice-recv for full DAVE E2EE support.

    Must be called once at startup, before any voice connections.
    Idempotent — safe to call multiple times.

    Returns True if applied, False if dependencies missing.
    """
    global _patched
    if _patched:
        return True

    # --- Check dependencies ---------------------------------------------------
    try:
        from davey import Codec, MediaType
    except ImportError:
        log.warning("davey not installed — DAVE voice will not work")
        return False

    try:
        from discord.ext.voice_recv import opus as recv_opus
    except ImportError:
        log.warning("discord-ext-voice-recv not installed — voice receive unavailable")
        return False

    import discord

    # =========================================================================
    # PATCH 1: Receive — DAVE decryption before opus decode
    # =========================================================================
    PacketDecoder = recv_opus.PacketDecoder
    VoiceData = recv_opus.VoiceData
    _debug = {"rx_count": 0, "decrypt_ok": 0, "decrypt_fail": 0, "no_user": 0, "not_ready": 0}
    _user_fail_counts: dict[int, int] = {}  # Track per-user decrypt failures

    def _process_packet_with_dave(self, packet):  # type: ignore[no-untyped-def]
        """Decrypt DAVE inner layer before opus decode."""
        # Skip non-audio packets
        if hasattr(packet, "payload") and packet.payload != 120:
            return VoiceData(packet, self._get_cached_member())

        if packet and packet.decrypted_data is not None:
            vc = self.sink.voice_client
            conn = getattr(vc, "_connection", None)
            dave_session = getattr(conn, "dave_session", None)
            dave_version = getattr(conn, "dave_protocol_version", 0)

            _debug["rx_count"] += 1
            if _debug["rx_count"] == 1:
                can_enc = getattr(conn, "can_encrypt", "N/A")
                log.info(
                    "DAVE recv: first packet, session=%s ready=%s can_encrypt=%s ver=%s",
                    dave_session is not None,
                    getattr(dave_session, "ready", "N/A"),
                    can_enc,
                    dave_version,
                )

            if dave_session is not None:
                if not dave_session.ready:
                    return VoiceData(packet, self._get_cached_member())

                # Enable passthrough for receive-side — allows unencrypted
                # frames through during DAVE epoch transitions. This MUST be
                # refreshed periodically because transitions happen whenever
                # users join/leave or the bot sends audio. A 10-second timeout
                # is too short — after 3-6 exchanges, passthrough expires and
                # the bot goes permanently deaf.
                # Refresh every 30 seconds to cover ongoing transitions.
                import time as _time
                _now = _time.monotonic()
                _last_pt = getattr(dave_session, '_last_passthrough_time', 0.0)
                if _now - _last_pt > 30.0:
                    try:
                        dave_session.set_passthrough_mode(True, 60)
                        dave_session._last_passthrough_time = _now
                    except Exception:
                        pass

                user_id = self._cached_id
                if user_id is None:
                    user_id = vc._get_id_from_ssrc(self.ssrc)
                    self._cached_id = user_id
                if user_id is None:
                    return VoiceData(packet, self._get_cached_member())

                try:
                    decrypted = dave_session.decrypt(
                        user_id,
                        MediaType.audio,
                        bytes(packet.decrypted_data),
                    )
                    packet.decrypted_data = decrypted
                    _debug["decrypt_ok"] += 1
                    _debug["consec_fail"] = 0
                    _user_fail_counts[user_id] = 0
                except Exception as exc:
                    _debug["decrypt_fail"] += 1
                    if _debug["decrypt_fail"] <= 3:
                        log.warning("DAVE decrypt error: %s", exc)

                    # Track consecutive failures — if too many in a row,
                    # the MLS group state is desynced and we need to reconnect
                    _debug.setdefault("consec_fail", 0)
                    _debug["consec_fail"] += 1

                    # Track per-user failures — if ANY user has 30+ consecutive
                    # failures, the MLS tree is desynced and reconnect is needed.
                    _user_fail_counts[user_id] = _user_fail_counts.get(user_id, 0) + 1
                    if _user_fail_counts[user_id] == 30:
                        log.warning(
                            "DAVE epoch desynced for user %s (%d failures), "
                            "requesting voice reconnect",
                            user_id, _user_fail_counts[user_id],
                        )
                        vc._dave_needs_reconnect = True
                        _user_fail_counts.clear()  # Reset after triggering

                    # CRITICAL: return early — do NOT pass encrypted data to
                    # opus decoder.
                    return VoiceData(packet, self._get_cached_member())

            elif dave_version and dave_version > 0:
                return VoiceData(packet, self._get_cached_member())

        # Opus decode (on now-decrypted data)
        pcm = None
        if not self.sink.wants_opus():
            try:
                packet, pcm = self._decode_packet(packet)
            except Exception:
                return VoiceData(packet, self._get_cached_member())

        member = self._get_cached_member()
        if member is None:
            self._cached_id = self.sink.voice_client._get_id_from_ssrc(self.ssrc)
            member = self._get_cached_member()

        data = VoiceData(packet, member, pcm=pcm)
        self._last_seq = packet.sequence
        self._last_ts = packet.timestamp
        return data

    PacketDecoder._process_packet = _process_packet_with_dave

    # =========================================================================
    # PATCH 2: Send — Thread-safe encrypt + exception guard + diagnostics
    # =========================================================================
    VoiceClient = discord.VoiceClient
    _original_get_voice_packet = VoiceClient._get_voice_packet
    _tx_debug = {"count": 0, "logged_state": False}

    def _get_voice_packet_safe(self, data: bytes):  # type: ignore[no-untyped-def]
        """Thread-safe _get_voice_packet with DAVE exception guard.

        If encrypt_opus() throws (race condition, uninitialized codec,
        key mismatch), we catch it instead of letting the AudioPlayer
        thread die silently. We also log the DAVE state on first call
        so we can diagnose can_encrypt issues.
        """
        conn = self._connection
        dave_session = conn.dave_session

        _tx_debug["count"] += 1
        if not _tx_debug["logged_state"]:
            _tx_debug["logged_state"] = True
            # Register SSRC with libdave encryptor (must happen before first encrypt)
            if dave_session and hasattr(dave_session, "set_ssrc") and hasattr(self, "ssrc"):
                try:
                    dave_session.set_ssrc(self.ssrc)
                    log.info("DAVE: registered SSRC %s with encryptor", self.ssrc)
                except Exception as e:
                    log.warning("DAVE: failed to set SSRC: %s", e)
            can_enc = conn.can_encrypt
            log.info(
                "DAVE send state: session=%s ready=%s can_encrypt=%s ver=%s epoch=%s",
                dave_session is not None,
                getattr(dave_session, "ready", "N/A"),
                can_enc,
                conn.dave_protocol_version,
                getattr(dave_session, "epoch", "N/A"),
            )
            if not can_enc:
                log.error(
                    "DAVE can_encrypt is FALSE — audio will be sent unencrypted "
                    "and silently dropped by all receivers!"
                )

        # Apply DAVE encryption with lock + exception guard.
        # Use explicit encrypt(MediaType.audio, Codec.opus, data) instead of
        # encrypt_opus(data) — the convenience wrapper in davey 0.1.4 may not
        # properly initialize the codec, producing frames receivers can't decrypt.
        if dave_session and conn.can_encrypt:
            try:
                packet = dave_session.encrypt_opus(data)
            except Exception as exc:
                if _tx_debug["count"] <= 5:
                    log.error("DAVE encrypt FAILED: %s", exc)
                packet = data

            # Log first encrypted frame for verification
            if _tx_debug["count"] == 1 and packet != data:
                has_marker = packet[-2:] == b'\xfa\xfa' if len(packet) >= 2 else False
                log.info(
                    "DAVE send: first frame in=%d out=%d 0xFAFA=%s",
                    len(data), len(packet), has_marker,
                )
        else:
            packet = data

        # Build RTP header + transport encryption (unchanged from original)
        import struct
        header = bytearray(12)
        header[0] = 0x80
        header[1] = 0x78
        struct.pack_into('>H', header, 2, self.sequence)
        struct.pack_into('>I', header, 4, self.timestamp)
        struct.pack_into('>I', header, 8, self.ssrc)

        encrypt_packet = getattr(self, '_encrypt_' + self.mode)
        return encrypt_packet(header, packet)

    # Send-side patch DISABLED — discord.py's original handles DAVE correctly.
    # But we need to register SSRC with LibdaveSession's encryptor.
    # Hook into play() to set SSRC before first audio packet.
    _original_play = VoiceClient.play

    def _play_with_ssrc(self, source, *, after=None):  # type: ignore[no-untyped-def]
        conn = self._connection
        ds = getattr(conn, "dave_session", None)
        if ds and hasattr(ds, "set_ssrc") and hasattr(self, "ssrc"):
            try:
                ds.set_ssrc(self.ssrc)
            except Exception:
                pass
        return _original_play(self, source, after=after)

    VoiceClient.play = _play_with_ssrc

    # =========================================================================
    # PATCH 3: Gateway — Extend passthrough on every DAVE epoch transition
    # =========================================================================
    # After process_commit() and process_welcome(), davey updates the MLS
    # tree but some incoming packets may arrive unencrypted during the
    # transition window. We must extend passthrough to cover this.
    try:
        from discord.gateway import DiscordVoiceWebSocket

        _original_received = DiscordVoiceWebSocket.received_message

        async def _received_message_with_passthrough(self, msg):  # type: ignore[no-untyped-def]
            result = await _original_received(self, msg)

            # After any binary message (MLS opcodes), extend passthrough
            if isinstance(msg, bytes) and len(msg) >= 3:
                conn = self._connection
                ds = getattr(conn, "dave_session", None)
                if ds and getattr(ds, "ready", False):
                    try:
                        ds.set_passthrough_mode(True, 60)
                    except Exception:
                        pass

            return result

        DiscordVoiceWebSocket.received_message = _received_message_with_passthrough
        log.info("Gateway passthrough extension patch applied")
    except Exception as exc:
        log.warning("Gateway patch failed (non-critical): %s", exc)

    _patched = True
    log.info("DAVE patches applied (receive decryption + gateway passthrough extension)")
    return True
