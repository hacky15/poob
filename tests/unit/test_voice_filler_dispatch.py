"""Tests for the Phase 2 filler-dispatch contract.

The Phase 2 ship (commit c470159) wired ``FillerPlayer`` into
``VoiceSession._process_single_response`` via ``_maybe_play_filler``.
Before the ship the ``FillerPlayer`` was instantiated at startup but
never actually fired — dead code from the original attempt.

These tests lock in the behavioral contract so a future refactor of
the response pipeline can't silently drop the filler-dispatch path
again. See ``docs/decisions/voice-latency-phase2-filler-dispatch.md``.

The tests bypass ``VoiceSession.__init__`` (which requires a full
voice_client/brain/stt/tts setup) and exercise the leaf method
``_maybe_play_filler`` directly. The structural contract that
``_process_single_response`` actually schedules the task is enforced
by source-grep here — that line is the load-bearing wire that
turned the filler player from dead code into the latency mask.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from poob.voice.fillers import FillerPlayer
from poob.voice.session import VoiceSession


# ---------------------------------------------------------------------------
# Behavioral contract for _maybe_play_filler
# ---------------------------------------------------------------------------

class TestMaybePlayFiller:
    """``_maybe_play_filler`` is the leaf called from the response pipeline.

    Contract:
      - When ``filler_player.available`` is False, do NOT call
        ``_play_audio``. (Startup didn't generate fillers, e.g. no
        Edge TTS quota — the bot stays silent on the latency mask.)
      - When ``filler_player.available`` is True and bytes load OK,
        DO call ``_play_audio`` with those bytes.
      - When bytes load returns ``None`` (e.g. read race / missing
        file), don't call ``_play_audio``.
      - All exception paths are swallowed (filler is fire-and-forget;
        never break the real response pipeline).
    """

    @pytest.fixture
    def session(self) -> VoiceSession:
        """Build a partial VoiceSession that bypasses ``__init__``.

        Only the attributes ``_maybe_play_filler`` touches are populated:
        ``filler_player`` and ``_play_audio``.
        """
        sess = VoiceSession.__new__(VoiceSession)
        sess.filler_player = FillerPlayer(filler_paths=[])
        sess._play_audio = AsyncMock()  # type: ignore[method-assign]
        return sess

    @pytest.mark.asyncio
    async def test_no_dispatch_when_filler_unavailable(
        self, session: VoiceSession,
    ) -> None:
        # No paths loaded — startup didn't generate fillers.
        session.filler_player = FillerPlayer(filler_paths=[])
        assert session.filler_player.available is False

        await session._maybe_play_filler()
        session._play_audio.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_dispatches_when_filler_available(
        self, session: VoiceSession, tmp_path: Path,
    ) -> None:
        # Stand up a fake filler file with real bytes.
        clip = tmp_path / "hmm.wav"
        clip.write_bytes(b"\x52\x49\x46\x46fake-wav-bytes")
        session.filler_player = FillerPlayer(filler_paths=[clip])
        assert session.filler_player.available is True

        await session._maybe_play_filler()
        session._play_audio.assert_called_once()  # type: ignore[attr-defined]
        played_arg = session._play_audio.call_args[0][0]  # type: ignore[attr-defined]
        assert played_arg == clip.read_bytes()

    @pytest.mark.asyncio
    async def test_no_dispatch_when_bytes_none(
        self, session: VoiceSession,
    ) -> None:
        # Filler reports available but bytes-load returns None (file
        # missing under us, read race, etc.). Must NOT call _play_audio
        # with None — that's the bug the early-return guards against.
        fake_player = MagicMock(spec=FillerPlayer)
        fake_player.available = True
        fake_player.get_filler_bytes = MagicMock(return_value=None)
        session.filler_player = fake_player

        await session._maybe_play_filler()
        session._play_audio.assert_not_called()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_play_exception_is_swallowed(
        self, session: VoiceSession, tmp_path: Path,
    ) -> None:
        # Filler is fire-and-forget — if _play_audio raises, the
        # method must NOT propagate; the real response pipeline
        # follows the filler and must not be poisoned by it.
        clip = tmp_path / "hmm.wav"
        clip.write_bytes(b"data")
        session.filler_player = FillerPlayer(filler_paths=[clip])
        session._play_audio = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("Discord voice client torn down"),
        )

        # Must not raise.
        await session._maybe_play_filler()


# ---------------------------------------------------------------------------
# Structural contract: _process_single_response wires the filler task
# ---------------------------------------------------------------------------

class TestProcessSingleResponseWiring:
    """The actual fix in c470159 was a single ``create_task`` call
    inside ``_process_single_response``. If a future refactor drops
    that line, the filler player goes back to being dead code with
    no test signal. This grep-as-test catches it.
    """

    def test_process_single_response_schedules_filler_task(self) -> None:
        import inspect

        source = inspect.getsource(VoiceSession._process_single_response)
        assert "_maybe_play_filler" in source, (
            "VoiceSession._process_single_response no longer dispatches "
            "the filler clip. This is the line that turned FillerPlayer "
            "from dead code into the Phase 2 latency mask — see "
            "docs/decisions/voice-latency-phase2-filler-dispatch.md."
        )
        assert "create_task" in source, (
            "Filler must be dispatched via create_task so it runs "
            "concurrently with the LLM/TTS path, not blocking it."
        )
