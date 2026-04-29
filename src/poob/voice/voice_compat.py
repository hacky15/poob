"""Runtime compatibility patches for Discord voice protocol changes (DAVE E2EE).

Monkeypatches py-cord 2.6.x voice internals to support the DAVE voice
handshake flow required by Discord voice servers since March 2026.

Based on the production-tested implementation from GabrielAgrela/Discord-Brain-Rot,
adapted for our voice pipeline.

Key patches:
- IDENTIFY payload includes max_dave_protocol_version
- Full MLS opcode handling (opcodes 21-31) for epoch transitions
- Binary websocket frame handling for MLS proposals/commits/welcomes
- unpack_audio() DAVE-decrypts incoming opus before feeding to Sink
- _get_voice_packet() DAVE-encrypts outgoing opus for playback
- Opcode 31 recovery for invalid MLS commits
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
from typing import Any, Dict, Tuple

import aiohttp
import discord
from discord import utils
from discord.enums import SpeakingState
from discord.errors import ConnectionClosed
from discord.gateway import DiscordVoiceWebSocket, VoiceKeepAliveHandler
from discord.sinks.core import RawData
from discord.voice_client import VoiceClient

try:
    import davey  # type: ignore

    _HAS_DAVEY = True
except Exception:
    davey = None  # type: ignore[assignment]
    _HAS_DAVEY = False


logger = logging.getLogger(__name__)


_PATCH_APPLIED = False
# key: (id(target), attr_name) -> (target, attr_name, had_attr, original_value)
_ORIGINALS: dict[Tuple[int, str], tuple[Any, str, bool, Any]] = {}


def _remember_attribute(target: Any, name: str) -> None:
    """Remember an attribute's original value once so tests can restore it."""
    key = (id(target), name)
    if key in _ORIGINALS:
        return
    had_attr = hasattr(target, name)
    value = getattr(target, name) if had_attr else None
    _ORIGINALS[key] = (target, name, had_attr, value)


def _set_attribute(target: Any, name: str, value: Any) -> None:
    """Set and track an attribute for later restoration."""
    _remember_attribute(target, name)
    setattr(target, name, value)


def _resolve_max_dave_protocol_version() -> int:
    """Resolve max DAVE protocol version from env with safe fallback."""
    raw = os.getenv("VOICE_MAX_DAVE_PROTOCOL_VERSION")
    if raw is not None:
        raw = raw.strip()
        try:
            return max(0, int(raw))
        except ValueError:
            logger.warning(
                "[VoiceCompat] Invalid VOICE_MAX_DAVE_PROTOCOL_VERSION=%r, using fallback",
                raw,
            )
    if _HAS_DAVEY:
        return max(0, int(getattr(davey, "DAVE_PROTOCOL_VERSION", 0)))
    return 0


def _ensure_ws_state(ws: DiscordVoiceWebSocket) -> None:
    """Initialize DAVE state attributes on DiscordVoiceWebSocket if missing."""
    if not hasattr(ws, "seq_ack"):
        ws.seq_ack = 0


def _ensure_voice_client_state(client: VoiceClient) -> None:
    """Initialize DAVE state attributes on VoiceClient if missing."""
    if not hasattr(client, "dave_session"):
        client.dave_session = None
    if not hasattr(client, "dave_protocol_version"):
        client.dave_protocol_version = 0
    if not hasattr(client, "dave_pending_transitions"):
        client.dave_pending_transitions = {}
    if not hasattr(client, "dave_downgraded"):
        client.dave_downgraded = False
    if not hasattr(client, "_voicecompat_active_ws"):
        client._voicecompat_active_ws = None


def _voiceclient_can_encrypt(self: VoiceClient) -> bool:
    """Return whether DAVE encryption is currently active and ready."""
    session = getattr(self, "dave_session", None)
    protocol = int(getattr(self, "dave_protocol_version", 0) or 0)
    return bool(protocol > 0 and session is not None and getattr(session, "ready", False))


async def _voiceclient_reinit_dave_session(self: VoiceClient) -> None:
    """Create or refresh DAVE session state after voice protocol negotiation."""
    _ensure_voice_client_state(self)
    version = int(getattr(self, "dave_protocol_version", 0) or 0)

    if version > 0:
        if not _HAS_DAVEY:
            raise RuntimeError(
                "davey library is required for DAVE voice protocol but is not installed"
            )
        if self.dave_session is not None:
            self.dave_session.reinit(version, int(self.user.id), int(self.channel.id))
        else:
            self.dave_session = davey.DaveSession(
                version, int(self.user.id), int(self.channel.id)
            )

        ws = getattr(self, "ws", None)
        if ws in (None, utils.MISSING):
            ws = getattr(self, "_voicecompat_active_ws", None)
        if ws in (None, utils.MISSING):
            logger.warning(
                "[VoiceCompat] DAVE session ready but websocket reference is unavailable"
            )
            return

        await ws.send_binary(
            DiscordVoiceWebSocket.MLS_KEY_PACKAGE,
            self.dave_session.get_serialized_key_package(),
        )
    elif self.dave_session is not None:
        self.dave_session.reset()
        self.dave_session.set_passthrough_mode(True, 10)


async def _voiceclient_recover_from_invalid_commit(
    self: VoiceClient, transition_id: int
) -> None:
    """Notify voice gateway of invalid MLS commit and restart DAVE session."""
    logger.warning("[VoiceCompat] Recovering from invalid MLS commit (transition=%s)",
                   transition_id)
    payload = {
        "op": DiscordVoiceWebSocket.MLS_INVALID_COMMIT_WELCOME,
        "d": {"transition_id": int(transition_id)},
    }
    await self.ws.send_as_json(payload)
    await self.reinit_dave_session()


async def _voiceclient_execute_transition(self: VoiceClient, transition_id: int) -> None:
    """Apply negotiated DAVE transition versions."""
    _ensure_voice_client_state(self)
    if transition_id not in self.dave_pending_transitions:
        logger.warning(
            "[VoiceCompat] Missing pending DAVE transition for id=%s", transition_id
        )
        return

    old_version = int(getattr(self, "dave_protocol_version", 0) or 0)
    self.dave_protocol_version = int(self.dave_pending_transitions.pop(transition_id))

    if old_version != self.dave_protocol_version and self.dave_protocol_version == 0:
        self.dave_downgraded = True
        logger.debug("[VoiceCompat] DAVE session downgraded")
    elif transition_id > 0 and self.dave_downgraded:
        self.dave_downgraded = False
        if self.dave_session is not None:
            self.dave_session.set_passthrough_mode(True, 10)
        logger.debug("[VoiceCompat] DAVE session upgraded")


# ---------------------------------------------------------------------------
# Patched WebSocket methods
# ---------------------------------------------------------------------------

async def _patched_identify(self: DiscordVoiceWebSocket) -> None:
    """Send voice IDENTIFY payload with DAVE negotiation field."""
    state = self._connection
    payload: Dict[str, Any] = {
        "op": self.IDENTIFY,
        "d": {
            "server_id": str(state.server_id),
            "user_id": str(state.user.id),
            "session_id": state.session_id,
            "token": state.token,
            "max_dave_protocol_version": int(
                getattr(state, "max_dave_protocol_version", 0)
            ),
        },
    }
    await self.send_as_json(payload)


async def _patched_speak(
    self: DiscordVoiceWebSocket, state: SpeakingState = SpeakingState.voice
) -> None:
    """Send speaking state while including SSRC for modern voice servers."""
    payload: Dict[str, Any] = {
        "op": self.SPEAKING,
        "d": {
            "speaking": int(state),
            "delay": 0,
        },
    }
    ssrc = getattr(self._connection, "ssrc", None)
    if ssrc not in (None, utils.MISSING):
        payload["d"]["ssrc"] = int(ssrc)
    await self.send_as_json(payload)


async def _patched_send_binary(
    self: DiscordVoiceWebSocket, opcode: int, data: bytes
) -> None:
    """Send raw binary voice websocket frame for MLS/DAVE opcodes."""
    logger.debug(
        "[VoiceCompat] Sending binary frame opcode=%s size=%s", opcode, len(data)
    )
    await self.ws.send_bytes(bytes([int(opcode)]) + data)


async def _patched_send_transition_ready(
    self: DiscordVoiceWebSocket, transition_id: int
) -> None:
    """Acknowledge DAVE transition readiness to the voice gateway."""
    payload = {
        "op": self.DAVE_TRANSITION_READY,
        "d": {"transition_id": int(transition_id)},
    }
    await self.send_as_json(payload)


async def _patched_load_secret_key(
    self: DiscordVoiceWebSocket, data: Dict[str, Any]
) -> None:
    """Store negotiated RTP key and send non-speaking packet with SSRC."""
    logger.info("[VoiceCompat] Received voice secret key")
    self.secret_key = self._connection.secret_key = data.get("secret_key")
    await self.speak(SpeakingState.none)


async def _patched_received_binary_message(
    self: DiscordVoiceWebSocket, msg: bytes
) -> None:
    """Handle binary MLS frames required for DAVE session management."""
    _ensure_ws_state(self)
    if len(msg) < 3:
        return

    self.seq_ack = struct.unpack_from(">H", msg, 0)[0]
    op = msg[2]
    state: VoiceClient = self._connection
    _ensure_voice_client_state(state)

    session = state.dave_session
    if session is None or not _HAS_DAVEY:
        return

    if op == self.MLS_EXTERNAL_SENDER:
        session.set_external_sender(msg[3:])
    elif op == self.MLS_PROPOSALS:
        if len(msg) < 4:
            return
        optype = msg[3]
        result = session.process_proposals(
            davey.ProposalsOperationType.append
            if optype == 0
            else davey.ProposalsOperationType.revoke,
            msg[4:],
        )
        if isinstance(result, davey.CommitWelcome):
            commit = result.commit
            welcome = result.welcome if result.welcome else b""
            await self.send_binary(self.MLS_COMMIT_WELCOME, commit + welcome)
    elif op == self.MLS_ANNOUNCE_COMMIT_TRANSITION:
        if len(msg) < 5:
            return
        transition_id = struct.unpack_from(">H", msg, 3)[0]
        try:
            session.process_commit(msg[5:])
            if transition_id != 0:
                state.dave_pending_transitions[transition_id] = int(
                    getattr(state, "dave_protocol_version", 0) or 0
                )
                await self.send_transition_ready(transition_id)
        except Exception:
            await state._recover_from_invalid_commit(transition_id)
    elif op == self.MLS_WELCOME:
        if len(msg) < 5:
            return
        transition_id = struct.unpack_from(">H", msg, 3)[0]
        try:
            session.process_welcome(msg[5:])
            if transition_id != 0:
                state.dave_pending_transitions[transition_id] = int(
                    getattr(state, "dave_protocol_version", 0) or 0
                )
                await self.send_transition_ready(transition_id)
        except Exception:
            await state._recover_from_invalid_commit(transition_id)


async def _patched_received_message(
    self: DiscordVoiceWebSocket, msg: Dict[str, Any]
) -> None:
    """Handle modern DAVE-aware voice websocket opcodes."""
    _ensure_ws_state(self)
    logger.debug("Voice websocket frame received: %s", msg)
    op = msg.get("op")
    data: Dict[str, Any] = msg.get("d") or {}
    self.seq_ack = msg.get("seq", self.seq_ack)
    # py-cord sets VoiceClient.ws only after connect_websocket() returns.
    # During handshake frames we need an early reference for DAVE key package send.
    self._connection._voicecompat_active_ws = self

    if op == self.READY:
        await self.initial_connection(data)
    elif op == self.HEARTBEAT_ACK:
        if self._keep_alive:
            self._keep_alive.ack()
    elif op == self.RESUMED:
        logger.info("Voice RESUME succeeded.")
    elif op == self.SESSION_DESCRIPTION:
        self._connection.mode = data["mode"]
        await self.load_secret_key(data)

        state: VoiceClient = self._connection
        _ensure_voice_client_state(state)
        state.dave_protocol_version = int(data.get("dave_protocol_version", 0) or 0)
        await state.reinit_dave_session()
    elif op == self.HELLO:
        interval = data["heartbeat_interval"] / 1000.0
        self._keep_alive = VoiceKeepAliveHandler(ws=self, interval=min(interval, 5.0))
        self._keep_alive.start()
    elif op == self.SPEAKING:
        ssrc = data["ssrc"]
        user = int(data["user_id"])
        speaking = data["speaking"]
        if ssrc in self.ssrc_map:
            self.ssrc_map[ssrc]["speaking"] = speaking
        else:
            self.ssrc_map.update({ssrc: {"user_id": user, "speaking": speaking}})
    elif op == self.DAVE_PREPARE_TRANSITION:
        state = self._connection
        _ensure_voice_client_state(state)
        transition_id = int(data["transition_id"])
        protocol_version = int(data["protocol_version"])
        logger.debug(
            "[VoiceCompat] Preparing DAVE transition id=%s protocol=%s",
            transition_id,
            protocol_version,
        )
        state.dave_pending_transitions[transition_id] = protocol_version
        if transition_id == 0:
            await state._execute_transition(transition_id)
        else:
            if protocol_version == 0 and state.dave_session is not None:
                state.dave_session.set_passthrough_mode(True, 120)
            await self.send_transition_ready(transition_id)
    elif op == self.DAVE_EXECUTE_TRANSITION:
        transition_id = int(data["transition_id"])
        logger.debug("[VoiceCompat] Executing DAVE transition id=%s", transition_id)
        await self._connection._execute_transition(transition_id)
    elif op == self.DAVE_PREPARE_EPOCH:
        epoch = int(data["epoch"])
        logger.debug("[VoiceCompat] Preparing DAVE epoch=%s", epoch)
        if epoch == 1:
            state = self._connection
            _ensure_voice_client_state(state)
            state.dave_protocol_version = int(data["protocol_version"])
            await state.reinit_dave_session()

    await self._hook(self, msg)


async def _patched_poll_event(self: DiscordVoiceWebSocket) -> None:
    """Poll voice websocket and process text or binary frames."""
    _ensure_ws_state(self)
    msg = await asyncio.wait_for(self.ws.receive(), timeout=30.0)
    if msg.type is aiohttp.WSMsgType.TEXT:
        await self.received_message(utils._from_json(msg.data))
    elif msg.type is aiohttp.WSMsgType.BINARY:
        await self.received_binary_message(msg.data)
    elif msg.type is aiohttp.WSMsgType.ERROR:
        logger.debug("Received voice websocket error frame: %s", msg)
        raise ConnectionClosed(self.ws, shard_id=None) from msg.data
    elif msg.type in (
        aiohttp.WSMsgType.CLOSED,
        aiohttp.WSMsgType.CLOSE,
        aiohttp.WSMsgType.CLOSING,
    ):
        close_code = getattr(self, "_close_code", None) or self.ws.close_code
        logger.warning(
            "[VoiceCompat] Voice websocket closed: code=%s reason=%s type=%s",
            close_code,
            getattr(msg, "extra", None),
            msg.type,
        )
        raise ConnectionClosed(self.ws, shard_id=None, code=close_code)


# ---------------------------------------------------------------------------
# Patched unpack_audio: DAVE-decrypt incoming audio for Sink pipeline
# ---------------------------------------------------------------------------

def _patched_unpack_audio(self: VoiceClient, data: bytes) -> None:
    """Decode incoming RTP audio, with DAVE decrypt for encrypted opus payloads.

    py-cord handles RTP transport encryption but does not decrypt DAVE media
    payloads for receive sinks. Without this, STT sees encrypted opus and
    decode fails continuously.
    """
    if data[1] & 0x78 != 0x78:
        return
    if self.paused:
        return

    frame = RawData(data, self)
    if frame.decrypted_data == b"\xf8\xff\xfe":  # Frame of silence
        return

    _ensure_voice_client_state(self)
    session = getattr(self, "dave_session", None)
    if session is not None and getattr(self, "can_encrypt", False):
        ws = getattr(self, "ws", None)
        if ws in (None, utils.MISSING):
            return

        user_id = ws.ssrc_map.get(frame.ssrc, {}).get("user_id")
        if user_id is None:
            return

        try:
            frame.decrypted_data = session.decrypt(
                int(user_id),
                davey.MediaType.audio,
                bytes(frame.decrypted_data),
            )
        except Exception:
            logger.debug(
                "[VoiceCompat] DAVE decrypt failed (ssrc=%s user=%s)",
                frame.ssrc,
                user_id,
                exc_info=True,
            )
            return

    self.decoder.decode(frame)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def apply_voice_compat_patches() -> None:
    """Apply runtime monkey patches for py-cord DAVE voice protocol compatibility.

    Safe to call multiple times (idempotent).
    """
    global _PATCH_APPLIED
    if _PATCH_APPLIED:
        return

    # Opt-in DEBUG-level voice_compat logging via env var. Off by
    # default to keep production log volume manageable. When
    # investigating DAVE handshake failures, set VOICE_COMPAT_DEBUG=1
    # to surface the binary-frame opcode trace and gateway-message
    # details — needed to distinguish empty-channel-first-join,
    # protocol drift, and stale MLS state.
    # See docs/research/dave-handshake-failure-april2026.md.
    if os.getenv("VOICE_COMPAT_DEBUG", "").strip().lower() in {"1", "true", "yes"}:
        logger.setLevel(logging.DEBUG)
        logger.info(
            "[VoiceCompat] DEBUG logging enabled via VOICE_COMPAT_DEBUG"
        )

    max_dave_protocol_version = _resolve_max_dave_protocol_version()

    # DAVE opcode constants
    dave_constants = {
        "DAVE_PREPARE_TRANSITION": 21,
        "DAVE_EXECUTE_TRANSITION": 22,
        "DAVE_TRANSITION_READY": 23,
        "DAVE_PREPARE_EPOCH": 24,
        "MLS_EXTERNAL_SENDER": 25,
        "MLS_KEY_PACKAGE": 26,
        "MLS_PROPOSALS": 27,
        "MLS_COMMIT_WELCOME": 28,
        "MLS_ANNOUNCE_COMMIT_TRANSITION": 29,
        "MLS_WELCOME": 30,
        "MLS_INVALID_COMMIT_WELCOME": 31,
    }
    for name, value in dave_constants.items():
        _set_attribute(DiscordVoiceWebSocket, name, value)

    # VoiceClient DAVE properties and methods
    if not hasattr(VoiceClient, "max_dave_protocol_version"):
        @property
        def max_dave_protocol_version_prop(self: VoiceClient) -> int:
            return max_dave_protocol_version
        _set_attribute(
            VoiceClient, "max_dave_protocol_version", max_dave_protocol_version_prop
        )

    if not hasattr(VoiceClient, "can_encrypt"):
        _set_attribute(VoiceClient, "can_encrypt", property(_voiceclient_can_encrypt))

    if not hasattr(VoiceClient, "reinit_dave_session"):
        _set_attribute(VoiceClient, "reinit_dave_session", _voiceclient_reinit_dave_session)

    if not hasattr(VoiceClient, "_recover_from_invalid_commit"):
        _set_attribute(
            VoiceClient,
            "_recover_from_invalid_commit",
            _voiceclient_recover_from_invalid_commit,
        )

    if not hasattr(VoiceClient, "_execute_transition"):
        _set_attribute(VoiceClient, "_execute_transition", _voiceclient_execute_transition)

    # Patch VoiceClient.__init__ to initialize DAVE state
    original_init = VoiceClient.__init__

    def patched_voice_client_init(self: VoiceClient, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        _ensure_voice_client_state(self)

    _set_attribute(VoiceClient, "__init__", patched_voice_client_init)

    # Patch outgoing DAVE encryption
    original_get_voice_packet = VoiceClient._get_voice_packet

    def patched_get_voice_packet(self: VoiceClient, data: bytes) -> bytes:
        _ensure_voice_client_state(self)
        packet = data
        session = getattr(self, "dave_session", None)
        if session is not None and getattr(self, "can_encrypt", False):
            try:
                packet = session.encrypt_opus(data)
            except Exception:
                logger.exception("[VoiceCompat] Failed to DAVE-encrypt opus packet")
        return original_get_voice_packet(self, packet)

    _set_attribute(VoiceClient, "_get_voice_packet", patched_get_voice_packet)

    # Patch incoming DAVE decryption for receive sinks
    _set_attribute(VoiceClient, "unpack_audio", _patched_unpack_audio)

    # Patch WebSocket from_client to initialize DAVE state on new connections
    original_from_client = DiscordVoiceWebSocket.from_client

    @classmethod
    async def patched_from_client(cls, client, *, resume=False, hook=None):
        ws = await original_from_client.__func__(cls, client, resume=resume, hook=hook)
        _ensure_ws_state(ws)
        return ws

    _set_attribute(DiscordVoiceWebSocket, "from_client", patched_from_client)

    # Patch WebSocket protocol methods
    _set_attribute(DiscordVoiceWebSocket, "identify", _patched_identify)
    _set_attribute(DiscordVoiceWebSocket, "speak", _patched_speak)
    _set_attribute(DiscordVoiceWebSocket, "send_binary", _patched_send_binary)
    _set_attribute(
        DiscordVoiceWebSocket, "send_transition_ready", _patched_send_transition_ready
    )
    _set_attribute(DiscordVoiceWebSocket, "load_secret_key", _patched_load_secret_key)
    _set_attribute(
        DiscordVoiceWebSocket,
        "received_binary_message",
        _patched_received_binary_message,
    )
    _set_attribute(DiscordVoiceWebSocket, "received_message", _patched_received_message)
    _set_attribute(DiscordVoiceWebSocket, "poll_event", _patched_poll_event)

    _PATCH_APPLIED = True
    logger.info(
        "[VoiceCompat] Applied py-cord DAVE voice patches "
        "(max_dave=%s, has_davey=%s, pycord=%s)",
        max_dave_protocol_version,
        _HAS_DAVEY,
        discord.__version__,
    )
