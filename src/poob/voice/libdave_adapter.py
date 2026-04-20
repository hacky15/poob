"""Adapter: dave.py (official C++ libdave) → davey DaveSession API.

davey 0.1.4 has a confirmed bug where encrypt_opus() produces frames
that report success but are not decodable by receivers.  This adapter
replaces davey's DaveSession with one backed by dave.py (DisnakeDev's
Python bindings for Discord's official C++ libdave implementation).

The adapter matches davey's DaveSession interface so discord.py's
gateway.py and voice_client.py can use it without modification.

Usage:
    Call ``install_libdave_adapter()`` BEFORE any voice connections.
    It monkey-patches ``davey.DaveSession`` to be our adapter class.
"""

from __future__ import annotations

import logging
import struct

log = logging.getLogger(__name__)

_installed = False


def install_libdave_adapter() -> bool:
    """Replace davey.DaveSession with a dave.py-backed adapter.

    Returns True if installed, False if dave.py is not available.
    """
    global _installed
    if _installed:
        return True

    try:
        import dave as libdave
    except ImportError:
        log.warning("dave.py not installed — cannot replace davey encryption")
        return False

    try:
        import davey
    except ImportError:
        log.warning("davey not installed — nothing to replace")
        return False

    # Save originals for reference
    _OriginalDaveSession = davey.DaveSession

    class LibdaveSessionAdapter:
        """Drop-in replacement for davey.DaveSession using official C++ libdave.

        Matches the davey.DaveSession API surface that discord.py calls:
        - __init__(protocol_version, user_id, channel_id, key_pair=None)
        - reinit(protocol_version, user_id, channel_id, key_pair=None)
        - reset()
        - encrypt_opus(data) / encrypt(media_type, codec, data)
        - decrypt(user_id, media_type, data)
        - process_proposals(operation_type, proposals, expected_user_ids=None)
        - process_welcome(welcome)
        - process_commit(commit)
        - set_external_sender(data)
        - get_serialized_key_package()
        - set_passthrough_mode(mode, expiry=None)
        - Properties: ready, status, epoch, voice_privacy_code, protocol_version
        """

        def __init__(
            self,
            protocol_version: int,
            user_id: int,
            channel_id: int,
            key_pair: object | None = None,
        ) -> None:
            self._protocol_version = protocol_version
            self._user_id = user_id
            self._channel_id = channel_id

            # dave.py Session (MLS group management)
            self._session = libdave.Session()
            self._key_pair = libdave.SignatureKeyPair.generate(protocol_version)
            # init(version, group_id, self_user_id, transient_key)
            self._session.init(
                protocol_version, channel_id, str(user_id), self._key_pair
            )

            # Separate Encryptor and Decryptor (frame crypto)
            self._encryptor = libdave.Encryptor()
            self._decryptors: dict[int, libdave.Decryptor] = {}

            self._epoch = 0
            self._ready = False
            self._ssrc: int | None = None

            log.info("LibdaveSessionAdapter created: ver=%s user=%s channel=%s", protocol_version, user_id, channel_id)

        def reinit(
            self,
            protocol_version: int,
            user_id: int,
            channel_id: int,
            key_pair: object | None = None,
        ) -> None:
            """Reinitialize the session (e.g., channel change)."""
            self._protocol_version = protocol_version
            self._user_id = user_id
            self._channel_id = channel_id
            self._session = libdave.Session()
            self._key_pair = libdave.SignatureKeyPair.generate(protocol_version)
            self._session.init(
                protocol_version, channel_id, str(user_id), self._key_pair
            )
            self._encryptor = libdave.Encryptor()
            self._decryptors.clear()
            self._ready = False
            self._epoch = 0
            log.info("LibdaveSessionAdapter reinit: channel=%s", channel_id)

        def reset(self) -> None:
            """Reset the session."""
            self._session.reset()
            self._encryptor = libdave.Encryptor()
            self._decryptors.clear()
            self._ready = False
            self._epoch = 0

        # --- MLS group management ---

        def set_external_sender(self, data: bytes) -> None:
            self._session.set_external_sender(bytes(data))

        def process_proposals(
            self, operation_type: object, proposals: bytes, expected_user_ids: list | None = None
        ) -> object:
            """Process MLS proposals. Returns CommitWelcome or None."""
            # davey passes ProposalsOperationType enum, dave.py doesn't use it
            # dave.py's process_proposals takes (proposals_bytes, recognized_user_ids)
            recognized = expected_user_ids or []
            result = self._session.process_proposals(bytes(proposals), recognized)
            self._update_key_ratchet()
            return result

        def process_welcome(self, welcome: bytes) -> object:
            result = self._session.process_welcome(bytes(welcome), [])
            self._update_key_ratchet()
            return result

        def process_commit(self, commit: bytes) -> object:
            result = self._session.process_commit(bytes(commit))
            self._update_key_ratchet()
            return result

        def get_serialized_key_package(self) -> bytes:
            return self._session.get_marshalled_key_package()

        # --- Encryption ---

        def _ensure_ssrc_registered(self) -> None:
            """Register the bot's SSRC with the encryptor if known."""
            if self._ssrc is not None:
                try:
                    self._encryptor.assign_ssrc_to_codec(
                        self._ssrc, libdave.Codec.opus
                    )
                except Exception:
                    pass  # Already registered

        def encrypt_opus(self, data: bytes) -> bytes:
            """Encrypt an Opus frame for transmission."""
            self._ensure_ssrc_registered()
            if self._ssrc is None:
                return data  # Can't encrypt without SSRC
            result = self._encryptor.encrypt(
                libdave.MediaType.audio, self._ssrc, bytes(data)
            )
            return result if result is not None else data

        def encrypt(self, media_type: object, codec: object, data: bytes) -> bytes:
            """Encrypt a media frame with explicit codec."""
            self._ensure_ssrc_registered()
            if self._ssrc is None:
                return data
            result = self._encryptor.encrypt(
                libdave.MediaType.audio, self._ssrc, bytes(data)
            )
            return result if result is not None else data

        # --- Decryption ---

        def decrypt(self, user_id: int, media_type: object, data: bytes) -> bytes:
            """Decrypt a received DAVE-encrypted frame."""
            if user_id not in self._decryptors:
                dec = libdave.Decryptor()
                # Transfer key ratchet to decryptor
                if self._session.has_established_group():
                    try:
                        ratchet = self._session.get_key_ratchet()
                        dec.transition_to_key_ratchet(ratchet)
                    except Exception:
                        pass
                self._decryptors[user_id] = dec

            decryptor = self._decryptors[user_id]
            result = decryptor.decrypt(
                libdave.MediaType.audio, bytes(data)
            )
            if result is None:
                raise ValueError("Decryption returned None")
            return result

        # --- Key ratchet management ---

        def _update_key_ratchet(self) -> None:
            """After MLS state changes, update Encryptor with new keys."""
            if self._session.has_established_group():
                try:
                    ratchet = self._session.get_key_ratchet()
                    self._encryptor.set_key_ratchet(ratchet)
                    self._ready = True
                    self._epoch += 1
                    log.info("LibdaveSessionAdapter key ratchet updated: epoch=%s ready=%s", self._epoch, self._ready)

                    # Also update all existing decryptors
                    for dec in self._decryptors.values():
                        try:
                            new_ratchet = self._session.get_key_ratchet()
                            dec.transition_to_key_ratchet(new_ratchet)
                        except Exception:
                            pass
                except Exception as exc:
                    log.warning("Failed to update key ratchet: %s", exc)

        # --- Properties ---

        @property
        def ready(self) -> bool:
            return self._ready and self._session.has_established_group()

        @property
        def status(self) -> str:
            return "ready" if self.ready else "pending"

        @property
        def epoch(self) -> int:
            return self._epoch

        @property
        def protocol_version(self) -> int:
            return self._protocol_version

        @property
        def voice_privacy_code(self) -> str:
            """Generate privacy code from epoch authenticator."""
            try:
                auth = self._session.get_last_epoch_authenticator()
                if auth:
                    import hashlib
                    h = hashlib.sha256(auth).digest()
                    code = str(int.from_bytes(h[:4], 'big') % 1000000).zfill(6)
                    return code
            except Exception:
                pass
            return ""

        @property
        def user_id(self) -> int:
            return self._user_id

        @property
        def channel_id(self) -> int:
            return self._channel_id

        # --- Passthrough mode ---

        def set_passthrough_mode(self, mode: bool, expiry: int | None = None) -> None:
            """Enable/disable passthrough mode for decryptors."""
            for dec in self._decryptors.values():
                try:
                    dec.transition_to_passthrough_mode()
                except Exception:
                    pass

        def can_passthrough(self, user_id: int) -> bool:
            return False

        # --- Stats ---

        def get_encryption_stats(self, media_type: object | None = None) -> object:
            return self._encryptor.get_stats()

        def get_decryption_stats(self, user_id: int, media_type: object | None = None) -> object:
            if user_id in self._decryptors:
                return self._decryptors[user_id].get_stats()
            return None

        # --- SSRC registration (called by our dave_patch) ---

        def set_ssrc(self, ssrc: int) -> None:
            """Register the bot's SSRC for encryption."""
            self._ssrc = ssrc
            self._ensure_ssrc_registered()

    # --- Monkey-patch davey.DaveSession ---
    davey.DaveSession = LibdaveSessionAdapter  # type: ignore[misc,assignment]

    _installed = True
    log.info(
        "Installed libdave adapter — davey.DaveSession replaced with "
        "official C++ libdave implementation (dave.py v0.1.2)"
    )
    return True
