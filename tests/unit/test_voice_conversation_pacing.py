"""Tests for voice conversation pacing — synth-ahead pipeline + length latitude.

Two compounding defects made Poob's voice replies feel "snappy with 2s of
silence between sentences" (operator report 2026-06-11):

1. **Pacing** — the per-sentence synth+play loop was *serial*: sentence N+1's
   TTS round-trip only began after the loop body for N. A short jab plays
   faster (~0.5-1s) than the next sentence synthesizes (~1-1.5s) -> dead air.
   Fix: ``VoiceSession._stream_synth_and_play`` runs a producer that
   synthesizes N+1 while the consumer plays N (bounded queue, order-preserving,
   persona-aware). First-word latency is unchanged.

2. **Content** — the prompt rule hard-clamped to "1-2 sentences, ~1-25 words",
   contradicting ``config.voice_llm_max_tokens = 200`` ("2-4 sentences"). Fix:
   relax to latitude (read-the-room), no hardcoded word cap.

See docs/decisions/voice-synth-ahead-pipeline.md and the updated
docs/architecture/voice-architecture.md.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock

import pytest

from poob.brain.poob import VOICE_BOOB, VOICE_TOOB, _build_system_prompt
from poob.voice.session import VoiceSession

# ---------------------------------------------------------------------------
# Test scaffolding — a VoiceSession that bypasses __init__
# ---------------------------------------------------------------------------

def _make_session() -> VoiceSession:
    """Partial VoiceSession exercising only the synth-and-play leaf.

    ``_stream_synth_and_play`` touches: the three ``_synthesize*`` methods,
    ``_play_audio``, ``music_player``, and ``voice_client.is_playing``.
    """
    sess = VoiceSession.__new__(VoiceSession)
    sess.music_player = None
    vc = MagicMock()
    vc.is_playing = MagicMock(return_value=False)
    sess.voice_client = vc
    return sess


async def _gen(*items: str):
    for it in items:
        yield it


# ---------------------------------------------------------------------------
# Pacing: synth-ahead pipelining
# ---------------------------------------------------------------------------

class TestSynthAheadPipeline:
    @pytest.mark.asyncio
    async def test_next_sentence_synthesizes_while_current_plays(self) -> None:
        """The load-bearing contract: while sentence 1 is still playing, the
        TTS for sentence 2 has ALREADY been issued. The old serial loop would
        not call synth(2) until play(1) returned -> this asserts the gap is
        gone at the source."""
        sess = _make_session()
        synth_calls: list[str] = []
        play_release = asyncio.Event()

        async def fake_synth(text: str) -> bytes:
            synth_calls.append(text)
            return f"audio:{text}".encode()

        play_order: list[bytes] = []

        async def fake_play(audio: bytes, **_kw: object) -> None:
            play_order.append(audio)
            if audio == b"audio:one.":
                # Block the FIRST playback so we can inspect mid-flight.
                await play_release.wait()

        sess._synthesize = fake_synth  # type: ignore[method-assign]
        sess._play_audio = fake_play  # type: ignore[method-assign]

        task = asyncio.create_task(
            sess._stream_synth_and_play(_gen("one.", "two."), t_start=0.0),
        )

        # Let the producer run ahead while play(one.) is blocked.
        for _ in range(20):
            await asyncio.sleep(0.01)
            if "two." in synth_calls:
                break

        assert "two." in synth_calls, (
            "synth-ahead violated: sentence 2 was not synthesized while "
            "sentence 1 was still playing (serial regression)"
        )

        play_release.set()
        full = await task
        assert play_order == [b"audio:one.", b"audio:two."], "play order must be preserved"
        assert full.strip() == "one. two."

    @pytest.mark.asyncio
    async def test_falsy_audio_is_not_played(self) -> None:
        """A sentence whose synth returns no bytes is skipped, not played as
        silence."""
        sess = _make_session()
        played: list[bytes] = []

        async def fake_synth(text: str) -> bytes | None:
            return None if text == "skip." else f"a:{text}".encode()

        async def fake_play(audio: bytes, **_kw: object) -> None:
            played.append(audio)

        sess._synthesize = fake_synth  # type: ignore[method-assign]
        sess._play_audio = fake_play  # type: ignore[method-assign]

        full = await sess._stream_synth_and_play(
            _gen("keep.", "skip.", "also."), t_start=0.0,
        )
        assert played == [b"a:keep.", b"a:also."]
        # full_response still includes the spoken text (skip.'s text is kept
        # in the transcript even though its audio was empty — that's the
        # model's words, the TTS just failed to render them).
        assert "keep." in full and "also." in full


# ---------------------------------------------------------------------------
# Persona dispatch: VOICE_TOOB / VOICE_BOOB are routing signals, not speech
# ---------------------------------------------------------------------------

class TestPersonaDispatch:
    @pytest.mark.asyncio
    async def test_toob_sentinel_routes_to_toob_synth_and_is_not_spoken(self) -> None:
        sess = _make_session()
        poob_calls: list[str] = []
        toob_calls: list[str] = []
        boob_calls: list[str] = []
        played: list[bytes] = []

        async def mk(rec: list[str], tag: str):
            async def _f(text: str) -> bytes:
                rec.append(text)
                return f"{tag}:{text}".encode()
            return _f

        sess._synthesize = await mk(poob_calls, "poob")  # type: ignore[method-assign]
        sess._synthesize_toob = await mk(toob_calls, "toob")  # type: ignore[method-assign]
        sess._synthesize_boob = await mk(boob_calls, "boob")  # type: ignore[method-assign]

        async def fake_play(audio: bytes, **_kw: object) -> None:
            played.append(audio)

        sess._play_audio = fake_play  # type: ignore[method-assign]

        full = await sess._stream_synth_and_play(
            _gen(VOICE_TOOB, "your taste is trash.", "as expected."),
            t_start=0.0,
        )

        assert toob_calls == ["your taste is trash.", "as expected."]
        assert poob_calls == []
        # The sentinel must never reach TTS or the transcript text.
        assert VOICE_TOOB not in full
        assert played == [b"toob:your taste is trash.", b"toob:as expected."]

    @pytest.mark.asyncio
    async def test_boob_sentinel_routes_to_boob_synth(self) -> None:
        sess = _make_session()
        boob_calls: list[str] = []

        async def boob_synth(text: str) -> bytes:
            boob_calls.append(text)
            return b"b"

        async def noop_synth(text: str) -> bytes:
            return b"x"

        async def fake_play(audio: bytes, **_kw: object) -> None:
            pass

        sess._synthesize = noop_synth  # type: ignore[method-assign]
        sess._synthesize_toob = noop_synth  # type: ignore[method-assign]
        sess._synthesize_boob = boob_synth  # type: ignore[method-assign]
        sess._play_audio = fake_play  # type: ignore[method-assign]

        await sess._stream_synth_and_play(
            _gen(VOICE_BOOB, "hi there cutie.", "toob's side piece here."),
            t_start=0.0,
        )
        assert boob_calls == ["hi there cutie.", "toob's side piece here."]


# ---------------------------------------------------------------------------
# Structural: all three response paths share the one helper
# ---------------------------------------------------------------------------

class TestAllPathsUseSharedHelper:
    """Three methods consume ``respond_streaming`` and synth+play. Before the
    consolidation, two of them (``_process_utterance``,
    ``_drain_pending_utterances``) called ``_synthesize`` directly with no
    persona dispatch — a music sentinel through those paths would be spoken
    aloud. Routing all three through the shared helper fixes that uniformly
    AND gives every path the synth-ahead pipeline. This grep-as-test stops a
    future refactor from re-forking them."""

    @pytest.mark.parametrize(
        "method_name",
        ["_process_single_response", "_process_utterance", "_drain_pending_utterances"],
    )
    def test_path_delegates_to_stream_synth_and_play(self, method_name: str) -> None:
        method = getattr(VoiceSession, method_name)
        source = inspect.getsource(method)
        assert "_stream_synth_and_play" in source, (
            f"{method_name} no longer delegates to the shared synth-ahead "
            "helper — see docs/decisions/voice-synth-ahead-pipeline.md. "
            "Re-forking the synth+play loop reintroduces the inter-sentence "
            "gap and the missing persona dispatch."
        )

    def test_helper_does_not_synth_serially(self) -> None:
        """The helper must run producer/consumer concurrently (gather/queue),
        not await synth then await play in one body."""
        source = inspect.getsource(VoiceSession._stream_synth_and_play)
        assert "Queue" in source, "synth-ahead requires a hand-off queue"
        assert "gather" in source, "producer and consumer must run concurrently"


# ---------------------------------------------------------------------------
# Content: the length rule gives latitude instead of a hard clamp
# ---------------------------------------------------------------------------

class TestVoiceLengthLatitude:
    def test_rigid_word_clamp_is_gone(self) -> None:
        """The old '~1-25 words' clamp contradicted voice_llm_max_tokens=200
        and forced every reply into a snap jab."""
        for voice in (True, False):
            prompt = _build_system_prompt(5, voice=voice, with_tools=False)
            assert "~1-25 words" not in prompt
            assert "1-2 sentences, ~1-25 words" not in prompt

    def test_latitude_language_is_present(self) -> None:
        prompt = _build_system_prompt(5, voice=True, with_tools=False)
        lowered = prompt.lower()
        # Latitude to go fuller when warranted, plus the read-the-room framing.
        assert "read the room" in lowered
        assert ("three or four sentences" in lowered or "let it breathe" in lowered)
        # Still discourages rambling — we didn't swing to verbose-by-default.
        assert "monologue" in lowered
