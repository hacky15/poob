"""Tests for complete voice-session teardown — no leaked Deepgram resources.

Root cause (audit 2026-06-15): ``VoiceSession.cleanup()`` cancelled inflight
tasks and flushed buffers but NEVER called ``self._dual_pipeline.cleanup()``.
Three per-user Deepgram WebSockets stayed open ~an hour after the session
ended (logged 'Deepgram stream disconnected' at 04:04 for a session torn down
at 03:04). Compounding it, ``DeepgramStreamManager.cleanup()`` closed the
streams but never cancelled its own keepalive task — so even the existing
cleanup leaked a live task.

Fix: ``VoiceSession.cleanup()`` awaits ``_dual_pipeline.cleanup()`` and drops
the reference; ``DeepgramStreamManager.cleanup()`` cancels the keepalive task
before closing streams. The whole chain tears down.

See docs/incidents/deepgram-streams-leak-on-cleanup.md.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from poob.voice.dual_pipeline import DeepgramStreamManager
from poob.voice.session import VoiceSession


@pytest.mark.asyncio
async def test_deepgram_cleanup_cancels_keepalive_and_closes_streams() -> None:
    mgr = DeepgramStreamManager.__new__(DeepgramStreamManager)
    mgr._streams = {1: MagicMock(), 2: MagicMock()}

    async def _forever() -> None:
        await asyncio.sleep(3600)

    mgr._keepalive_task = asyncio.get_event_loop().create_task(_forever())
    keepalive_ref = mgr._keepalive_task
    mgr.close_user = AsyncMock()  # type: ignore[method-assign]

    await mgr.cleanup()

    assert mgr.close_user.await_count == 2, "every open stream must be closed"
    assert mgr._keepalive_task is None, "keepalive reference must be dropped"
    await asyncio.sleep(0)
    assert keepalive_ref.cancelled(), "keepalive task must be cancelled (was leaking)"


@pytest.mark.asyncio
async def test_deepgram_cleanup_safe_with_no_keepalive() -> None:
    """Idempotent / defensive: cleanup must not crash if no keepalive ran."""
    mgr = DeepgramStreamManager.__new__(DeepgramStreamManager)
    mgr._streams = {}
    mgr._keepalive_task = None
    mgr.close_user = AsyncMock()  # type: ignore[method-assign]

    await mgr.cleanup()  # must not raise

    assert mgr.close_user.await_count == 0


@pytest.mark.asyncio
async def test_voice_session_cleanup_tears_down_dual_pipeline() -> None:
    sess = VoiceSession.__new__(VoiceSession)
    sess._inflight_tasks = set()
    sess._user_buffers = {}
    sess._speech_detectors = {}
    sess.music_player = MagicMock()
    dp = MagicMock()
    dp.cleanup = AsyncMock()
    sess._dual_pipeline = dp

    await sess.cleanup()

    dp.cleanup.assert_awaited_once()
    assert sess._dual_pipeline is None, "dual pipeline reference must be dropped"


@pytest.mark.asyncio
async def test_voice_session_cleanup_safe_without_dual_pipeline() -> None:
    """Energy-VAD-only sessions have no dual pipeline — cleanup must not crash."""
    sess = VoiceSession.__new__(VoiceSession)
    sess._inflight_tasks = set()
    sess._user_buffers = {}
    sess._speech_detectors = {}
    sess.music_player = MagicMock()
    sess._dual_pipeline = None

    await sess.cleanup()  # must not raise

    assert sess._dual_pipeline is None


def test_cleanup_wiring_is_present_in_source() -> None:
    """Grep-as-test: lock the fix against a future refactor silently dropping
    either half of the teardown chain."""
    sess_src = inspect.getsource(VoiceSession.cleanup)
    assert "_dual_pipeline" in sess_src and "cleanup" in sess_src, (
        "VoiceSession.cleanup must tear down the dual pipeline — see "
        "docs/incidents/deepgram-streams-leak-on-cleanup.md."
    )
    mgr_src = inspect.getsource(DeepgramStreamManager.cleanup)
    assert "_keepalive_task" in mgr_src, (
        "DeepgramStreamManager.cleanup must cancel the keepalive task — it was "
        "leaking a live task after every session."
    )
