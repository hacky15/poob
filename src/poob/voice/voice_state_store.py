"""Persist Poob's active voice-channel membership across restarts.

So a redeploy (or crash-restart) auto-rejoins the channels Poob was in,
instead of the operator having to /join again. Stored as a small JSON map
``{guild_id: channel_id}`` on the data volume, so it survives container
recreation. See docs/decisions/voice-auto-rejoin-on-restart.md.

All operations are best-effort and never raise — a persistence failure
must never break joining/leaving or boot.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from poob.utils.logging import get_logger

log = get_logger("voice.state_store")


class VoiceStateStore:
    """Thread-safe JSON-file store of ``{guild_id: channel_id}``."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def record(self, guild_id: int, channel_id: int) -> None:
        """Mark that Poob is connected to ``channel_id`` in ``guild_id``."""
        with self._lock:
            data = self._read()
            if data.get(str(guild_id)) == channel_id:
                return
            data[str(guild_id)] = channel_id
            self._write(data)

    def forget(self, guild_id: int) -> None:
        """Drop the entry for ``guild_id`` (Poob left / disconnected)."""
        with self._lock:
            data = self._read()
            if data.pop(str(guild_id), None) is not None:
                self._write(data)

    def load(self) -> list[tuple[int, int]]:
        """Return the persisted ``(guild_id, channel_id)`` pairs."""
        with self._lock:
            data = self._read()
        out: list[tuple[int, int]] = []
        for g, c in data.items():
            try:
                out.append((int(g), int(c)))
            except (TypeError, ValueError):
                continue
        return out

    # -- internal (call under _lock) -----------------------------------
    def _read(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data), encoding="utf-8")
        except OSError as exc:
            log.warning("voice_state_store write failed", error=str(exc)[:80])
