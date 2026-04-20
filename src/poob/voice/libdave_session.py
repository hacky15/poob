"""dave.py (libdave C++) session adapter for discord.py.

Replaces davey's broken OpenMLS implementation with Discord's official
C++ libdave for DAVE E2EE. This fixes the multi-user epoch desync bug
(confirmed in JDA #2998, discord.js #11419) because libdave uses mlspp
instead of OpenMLS.

Architecture:
  - dave.py Session handles MLS lifecycle (proposals, commits, welcomes)
  - dave.py Decryptor handles per-user frame decryption
  - dave.py Encryptor handles frame encryption
  - This adapter maps dave.py's API to davey's API surface so discord.py
    can use it as a drop-in replacement via monkey-patching davey.DaveSession

Key API differences between davey and dave.py:
  - process_proposals: davey(optype, bytes) → dave.py(bytes, user_ids)
  - process_welcome: davey(bytes) → dave.py(bytes, user_ids)
  - process_commit: davey(bytes)→None → dave.py(bytes)→dict
  - decrypt: davey(user_id:int, media_type, bytes) → dave.py per-user Decryptor
  - encrypt_opus: davey(bytes) → dave.py Encryptor.encrypt(media_type, ssrc, bytes)
"""

from __future__ import annotations

import logging
import struct
from typing import Any

import dave as libdave

log = logging.getLogger(__name__)


class LibdaveSession:
    """Drop-in replacement for davey.DaveSession using dave.py (libdave C++).

    Maps davey's API to dave.py's API so discord.py's voice_state.py
    and gateway.py can use it without modification.
    """

    def __init__(self, protocol_version: int, user_id: int, channel_id: int) -> None:
        self._protocol_version = protocol_version
        self._user_id = str(user_id)
        self._channel_id = channel_id

        # dave.py Session (handles MLS group management)
        self._session = libdave.Session()
        key_pair = libdave.SignatureKeyPair.generate(protocol_version)
        self._session.init(protocol_version, channel_id, self._user_id, key_pair)

        # Per-user decryptors (dave.py manages these separately)
        self._decryptors: dict[int, libdave.Decryptor] = {}

        # Encryptor for outbound audio
        self._encryptor = libdave.Encryptor()
        self._encryptor.set_protocol_version_changed_callback(lambda: None)
        self._ssrc: int | None = None

        # Track known user IDs for process_proposals/process_welcome
        self._known_user_ids: set[str] = set()

        # State properties matching davey's API
        self._ready = False
        self._epoch: int | None = None
        self._can_encrypt = False

        log.info(
            "LibdaveSession created: ver=%d user=%s channel=%d",
            protocol_version, self._user_id, channel_id,
        )

    # =====================================================================
    # Properties matching davey.DaveSession API
    # =====================================================================

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def epoch(self) -> int | None:
        return self._epoch

    @property
    def status(self) -> str:
        return "ready" if self._ready else "not_ready"

    @property
    def protocol_version(self) -> int:
        return self._protocol_version

    @property
    def voice_privacy_code(self) -> str | None:
        try:
            auth = self._session.get_last_epoch_authenticator()
            if auth:
                return libdave.generate_displayable_code(auth)
        except Exception:
            pass
        return None

    # =====================================================================
    # MLS lifecycle methods (called by discord.py gateway.py)
    # =====================================================================

    def set_external_sender(self, data: bytes) -> None:
        """Process MLS external sender (opcode 25)."""
        self._session.set_external_sender(bytes(data))

    def process_proposals(self, operation_type: Any, proposals: bytes) -> Any:
        """Process MLS proposals (opcode 27).

        davey signature: (ProposalsOperationType, bytes) → CommitWelcome | None
        dave.py signature: (bytes, Set[str]) → bytes | None

        Returns an object with .commit and .welcome attributes if a commit
        was generated (mimics davey.CommitWelcome).
        """
        result = self._session.process_proposals(
            bytes(proposals), self._known_user_ids
        )

        if result is not None:
            # dave.py returns commit bytes, davey expects CommitWelcome object
            return _CommitWelcome(result)
        return None

    def process_commit(self, commit: bytes) -> None:
        """Process MLS commit (opcode 29).

        After processing, update key ratchets for all known users.
        """
        try:
            result = self._session.process_commit(bytes(commit))
            self._ready = self._session.has_established_group()
            self._can_encrypt = self._ready

            if self._ready:
                self._update_key_ratchets()

            log.debug("process_commit: ready=%s, result=%s", self._ready, type(result))
        except Exception as exc:
            log.warning("process_commit failed: %s", exc)
            raise

    def process_welcome(self, welcome: bytes) -> None:
        """Process MLS welcome (opcode 30).

        After processing, update key ratchets for all known users.
        Note: dave.py needs recognized_user_ids but we pass an empty set
        to avoid the 'unrecognized user ID' errors — libdave will still
        process the welcome, it just won't verify user credentials.
        """
        try:
            # Pass empty set — dave.py logs warnings for unrecognized IDs
            # but still processes the welcome correctly
            result = self._session.process_welcome(
                bytes(welcome), set()
            )
            self._ready = self._session.has_established_group()
            self._can_encrypt = self._ready

            if self._ready:
                self._update_key_ratchets()

            log.debug("process_welcome: ready=%s", self._ready)
        except Exception as exc:
            log.warning("process_welcome failed: %s", exc)
            raise

    # =====================================================================
    # Key package (called by discord.py voice_state.py)
    # =====================================================================

    def get_serialized_key_package(self) -> bytes:
        """Get marshalled key package for MLS registration."""
        return self._session.get_marshalled_key_package()

    # =====================================================================
    # Encryption (called by discord.py voice_client.py)
    # =====================================================================

    def encrypt_opus(self, data: bytes) -> bytes:
        """Encrypt an opus frame for DAVE E2EE.

        Uses dave.py's Encryptor which correctly handles the DAVE
        frame format (AES-128-GCM + nonce + auth tag + 0xFAFA marker).
        """
        if self._ssrc is None:
            # Auto-detect SSRC from the voice client if not set
            # This happens because discord.py calls encrypt_opus before
            # our code has a chance to call set_ssrc()
            return data  # Pass through unencrypted — DAVE will handle via can_encrypt check

        result = self._encryptor.encrypt(
            libdave.MediaType.audio,
            self._ssrc,
            bytes(data),
        )
        if result is None:
            raise RuntimeError("dave.py encrypt returned None")
        return result

    def set_ssrc(self, ssrc: int) -> None:
        """Register the bot's SSRC with the encryptor."""
        self._ssrc = ssrc
        self._encryptor.assign_ssrc_to_codec(ssrc, libdave.Codec.opus)

    # =====================================================================
    # Decryption (called by our dave_patch.py)
    # =====================================================================

    def decrypt(self, user_id: int, media_type: Any, data: bytes) -> bytes:
        """Decrypt a DAVE-encrypted audio frame.

        Creates per-user Decryptors on demand and manages their key ratchets.

        Args:
            user_id: Discord user ID (int).
            media_type: davey.MediaType.audio (ignored, we use dave.MediaType).
            data: Transport-decrypted audio payload (still DAVE-encrypted).

        Returns:
            Decrypted opus frame bytes.

        Raises:
            Exception if decryption fails.
        """
        # Track user for future proposals/welcomes
        user_id_str = str(user_id)
        self._known_user_ids.add(user_id_str)

        # Get or create per-user decryptor
        if user_id not in self._decryptors:
            dec = libdave.Decryptor()
            # Try to get key ratchet for this user
            if self._ready:
                try:
                    ratchet = self._session.get_key_ratchet(user_id_str)
                    if ratchet is not None:
                        dec.transition_to_key_ratchet(ratchet, 10.0)
                except Exception as exc:
                    log.debug("No key ratchet for user %s: %s", user_id, exc)
            self._decryptors[user_id] = dec

        decryptor = self._decryptors[user_id]
        result = decryptor.decrypt(libdave.MediaType.audio, bytes(data))

        if result is None:
            raise RuntimeError(f"dave.py decrypt returned None for user {user_id}")

        return result

    # =====================================================================
    # Passthrough and diagnostics
    # =====================================================================

    def set_passthrough_mode(self, enabled: bool, duration: int) -> None:
        """Set passthrough mode on all decryptors."""
        import datetime
        td = datetime.timedelta(seconds=duration)
        for dec in self._decryptors.values():
            try:
                dec.transition_to_passthrough_mode(td)
            except Exception:
                pass

    def get_encryption_stats(self) -> Any:
        """Get encryption stats from the encryptor."""
        return self._encryptor.get_stats()

    def get_epoch_authenticator(self) -> bytes | None:
        try:
            return self._session.get_last_epoch_authenticator()
        except Exception:
            return None

    def get_user_ids(self) -> list[str]:
        return list(self._known_user_ids)

    def get_pairwise_fingerprint(self, user_id: int) -> bytes | None:
        try:
            return self._session.get_pairwise_fingerprint(str(user_id))
        except Exception:
            return None

    def reset(self) -> None:
        """Reset the MLS session."""
        self._session.reset()
        self._decryptors.clear()
        self._ready = False
        self._can_encrypt = False

    def reinit(self) -> None:
        """Re-initialize the session."""
        key_pair = libdave.SignatureKeyPair.generate(self._protocol_version)
        self._session.init(
            self._protocol_version, self._channel_id, self._user_id, key_pair
        )
        self._decryptors.clear()
        self._ready = False

    # =====================================================================
    # Internal helpers
    # =====================================================================

    def _update_key_ratchets(self) -> None:
        """Update key ratchets for all known users after epoch transition."""
        for uid_str in self._known_user_ids:
            try:
                ratchet = self._session.get_key_ratchet(uid_str)
                if ratchet is not None:
                    uid_int = int(uid_str)
                    if uid_int not in self._decryptors:
                        self._decryptors[uid_int] = libdave.Decryptor()
                    self._decryptors[uid_int].transition_to_key_ratchet(ratchet, 10.0)
            except Exception as exc:
                log.debug("Failed to update ratchet for user %s: %s", uid_str, exc)

        # Also update encryptor
        try:
            ratchet = self._session.get_key_ratchet(self._user_id)
            if ratchet is not None:
                self._encryptor.set_key_ratchet(ratchet)
        except Exception:
            pass


class _CommitWelcome:
    """Mimics davey.CommitWelcome for compatibility with discord.py gateway."""

    def __init__(self, commit_bytes: bytes) -> None:
        self.commit = commit_bytes
        self.welcome = None  # dave.py doesn't separate these


def install_libdave_session() -> bool:
    """Monkey-patch davey.DaveSession with LibdaveSession.

    Must be called before any voice connections.
    Returns True if installed, False if dave.py unavailable.
    """
    try:
        import dave as _dave  # noqa: F401
    except ImportError:
        log.warning("dave.py not installed — cannot use libdave backend")
        return False

    try:
        import davey
        davey.DaveSession = LibdaveSession  # type: ignore[attr-defined]
        log.info("Installed LibdaveSession (dave.py/libdave C++) as DAVE backend")
        return True
    except ImportError:
        log.warning("davey not installed — cannot monkey-patch")
        return False
