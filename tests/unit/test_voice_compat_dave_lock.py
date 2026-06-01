"""Tests for the DAVE serialization-lock + reconnect-offload fix.

A WS 4014 reconnect re-runs the DAVE MLS handshake; the native davey
(Rust/FFI) re-key calls must run OFF the event loop and serialized
against the recv/player decrypt/encrypt threads, or a native deadlock
pins the loop and freezes the whole bot. See
docs/incidents/voice-4014-reconnect-event-loop-wedge.md and
docs/gotchas/davey-session-needs-serialization-lock.md.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import poob.voice.voice_compat as vc


@pytest.fixture(autouse=True)
def _mls_opcode(monkeypatch):
    """apply_voice_compat_patches() sets MLS_* opcode constants on the WS
    class at startup; unit tests don't run the patch, so provide the one
    the reinit path references."""
    monkeypatch.setattr(
        vc.DiscordVoiceWebSocket, "MLS_KEY_PACKAGE", 26, raising=False,
    )


def test_dave_lock_is_a_threading_lock() -> None:
    # Must be a threading.Lock (decrypt/encrypt run on real threads), not
    # an asyncio.Lock, and must exist (the migration had dropped it).
    assert isinstance(vc._dave_lock, threading.Lock().__class__)


def _fake_self(*, existing_session: bool, version: int = 1) -> SimpleNamespace:
    ws = MagicMock()
    ws.send_binary = AsyncMock()
    session = MagicMock()
    session.get_serialized_key_package.return_value = b"KEYPKG"
    return SimpleNamespace(
        dave_protocol_version=version,
        dave_session=session if existing_session else None,
        user=SimpleNamespace(id=111),
        channel=SimpleNamespace(id=222),
        ws=ws,
        _voicecompat_active_ws=ws,
        dave_pending_transitions={5: 1, 6: 1},
    )


@pytest.mark.asyncio
async def test_reinit_rekeys_clears_pending_and_sends_keypackage(monkeypatch) -> None:
    fake_davey = MagicMock()
    monkeypatch.setattr(vc, "davey", fake_davey)
    monkeypatch.setattr(vc, "_HAS_DAVEY", True)
    monkeypatch.setattr(vc, "_ensure_voice_client_state", lambda self: None)

    me = _fake_self(existing_session=True)
    await vc._voiceclient_reinit_dave_session(me)

    # Stale epoch transitions dropped before the rebuild.
    assert me.dave_pending_transitions == {}
    # Existing session → reinit (not reconstruct), with version/user/channel.
    me.dave_session.reinit.assert_called_once_with(1, 111, 222)
    me.dave_session.get_serialized_key_package.assert_called_once()
    fake_davey.DaveSession.assert_not_called()
    # Fresh key package sent to the gateway.
    me.ws.send_binary.assert_awaited_once()
    args = me.ws.send_binary.await_args.args
    assert args[1] == b"KEYPKG"


@pytest.mark.asyncio
async def test_reinit_constructs_session_when_absent(monkeypatch) -> None:
    new_session = MagicMock()
    new_session.get_serialized_key_package.return_value = b"NEWKP"
    fake_davey = MagicMock()
    fake_davey.DaveSession.return_value = new_session
    monkeypatch.setattr(vc, "davey", fake_davey)
    monkeypatch.setattr(vc, "_HAS_DAVEY", True)
    monkeypatch.setattr(vc, "_ensure_voice_client_state", lambda self: None)

    me = _fake_self(existing_session=False)
    await vc._voiceclient_reinit_dave_session(me)

    fake_davey.DaveSession.assert_called_once_with(1, 111, 222)
    assert me.dave_session is new_session
    me.ws.send_binary.assert_awaited_once()


@pytest.mark.asyncio
async def test_reinit_native_calls_run_under_the_lock(monkeypatch) -> None:
    """The native re-key must hold _dave_lock while it runs (so concurrent
    decrypt/encrypt on the recv/player threads can't race the MLS tree)."""
    monkeypatch.setattr(vc, "davey", MagicMock())
    monkeypatch.setattr(vc, "_HAS_DAVEY", True)
    monkeypatch.setattr(vc, "_ensure_voice_client_state", lambda self: None)

    me = _fake_self(existing_session=True)
    held = {"during_reinit": None}

    def _check_lock(*_a, **_k):
        # While reinit runs, the lock must be held (acquire() returns False).
        held["during_reinit"] = not vc._dave_lock.acquire(blocking=False)
        if not held["during_reinit"]:
            vc._dave_lock.release()

    me.dave_session.reinit.side_effect = _check_lock
    await vc._voiceclient_reinit_dave_session(me)
    assert held["during_reinit"] is True
    # And the lock is released afterward (no leak).
    assert vc._dave_lock.acquire(blocking=False) is True
    vc._dave_lock.release()


@pytest.mark.asyncio
async def test_reinit_downgrade_resets_under_lock(monkeypatch) -> None:
    # version 0 → passthrough/reset path (no key package send).
    monkeypatch.setattr(vc, "davey", MagicMock())
    monkeypatch.setattr(vc, "_HAS_DAVEY", True)
    monkeypatch.setattr(vc, "_ensure_voice_client_state", lambda self: None)

    me = _fake_self(existing_session=True, version=0)
    await vc._voiceclient_reinit_dave_session(me)

    me.dave_session.reset.assert_called_once()
    me.dave_session.set_passthrough_mode.assert_called_once()
    me.ws.send_binary.assert_not_awaited()
