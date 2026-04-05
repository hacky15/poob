"""Unit tests for the voice chat module."""

from __future__ import annotations

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentic_scraper.voice.audio_buffer import (
    FRAME_DURATION_MS,
    UserAudioBuffer,
    VADConfig,
    VADState,
    rms_energy,
)
from agentic_scraper.voice.conversation import VoiceConversationManager
from agentic_scraper.voice.stt import GroqWhisperSTT, _pcm_to_wav, _stereo_to_mono


# ---------------------------------------------------------------------------
# audio_buffer tests
# ---------------------------------------------------------------------------

class TestRmsEnergy:
    """Tests for RMS energy calculation."""

    def test_silence_returns_zero(self) -> None:
        silence = b"\x00" * 3840
        assert rms_energy(silence) == 0.0

    def test_loud_signal_returns_high(self) -> None:
        # Max amplitude 16-bit samples
        n_samples = 1920
        loud = struct.pack(f"<{n_samples}h", *([32000] * n_samples))
        assert rms_energy(loud) > 30000

    def test_moderate_signal(self) -> None:
        n_samples = 1920
        moderate = struct.pack(f"<{n_samples}h", *([500] * n_samples))
        energy = rms_energy(moderate)
        assert 400 < energy < 600

    def test_empty_data(self) -> None:
        assert rms_energy(b"") == 0.0

    def test_single_sample(self) -> None:
        single = struct.pack("<h", 1000)
        assert rms_energy(single) == 1000.0


class TestVADConfig:
    """Tests for VAD configuration."""

    def test_silence_frames(self) -> None:
        cfg = VADConfig(silence_duration_ms=700)
        assert cfg.silence_frames == 700 // FRAME_DURATION_MS  # 35

    def test_max_speech_frames(self) -> None:
        cfg = VADConfig(max_speech_duration_ms=30_000)
        assert cfg.max_speech_frames == 30_000 // FRAME_DURATION_MS  # 1500


class TestUserAudioBuffer:
    """Tests for per-user audio buffering with VAD."""

    def _make_speech_frame(self, amplitude: int = 500) -> bytes:
        """Create a 20ms frame of 48kHz stereo PCM with given amplitude."""
        n_samples = 3840 // 2  # 16-bit = 2 bytes per sample
        return struct.pack(f"<{n_samples}h", *([amplitude] * n_samples))

    def _make_silence_frame(self) -> bytes:
        return b"\x00" * 3840

    def test_starts_idle(self) -> None:
        buf = UserAudioBuffer(user_id=123)
        assert buf._state == VADState.IDLE

    def test_speech_detection_starts_buffering(self) -> None:
        buf = UserAudioBuffer(user_id=123, config=VADConfig(energy_threshold=100))
        frame = self._make_speech_frame(amplitude=500)
        buf.add_frame(frame)
        assert buf._state == VADState.SPEAKING
        assert len(buf._frames) == 1

    def test_silence_stays_idle(self) -> None:
        buf = UserAudioBuffer(user_id=123)
        frame = self._make_silence_frame()
        buf.add_frame(frame)
        assert buf._state == VADState.IDLE

    def test_utterance_emitted_after_silence(self) -> None:
        callback = MagicMock()
        cfg = VADConfig(
            energy_threshold=100,
            silence_duration_ms=60,  # 3 frames of silence
            min_speech_duration_ms=20,  # 1 frame min
        )
        buf = UserAudioBuffer(user_id=42, config=cfg, on_utterance=callback)

        # 5 frames of speech
        speech = self._make_speech_frame(500)
        for _ in range(5):
            buf.add_frame(speech)

        assert buf._state == VADState.SPEAKING
        callback.assert_not_called()

        # 3 frames of silence (= 60ms, triggers emit)
        silence = self._make_silence_frame()
        for _ in range(3):
            buf.add_frame(silence)

        callback.assert_called_once()
        user_id, pcm_data = callback.call_args[0]
        assert user_id == 42
        assert len(pcm_data) > 0
        assert buf._state == VADState.IDLE

    def test_short_utterance_discarded(self) -> None:
        callback = MagicMock()
        cfg = VADConfig(
            energy_threshold=100,
            silence_duration_ms=20,  # 1 frame
            min_speech_duration_ms=200,  # 10 frames required
        )
        buf = UserAudioBuffer(user_id=42, config=cfg, on_utterance=callback)

        # Only 2 frames of speech (too short)
        speech = self._make_speech_frame(500)
        buf.add_frame(speech)
        buf.add_frame(speech)

        # Silence triggers check
        silence = self._make_silence_frame()
        buf.add_frame(silence)

        # Callback should NOT be called (utterance too short)
        callback.assert_not_called()
        assert buf._state == VADState.IDLE

    def test_max_duration_cutoff(self) -> None:
        callback = MagicMock()
        cfg = VADConfig(
            energy_threshold=100,
            max_speech_duration_ms=100,  # 5 frames max
            min_speech_duration_ms=20,
        )
        buf = UserAudioBuffer(user_id=42, config=cfg, on_utterance=callback)

        speech = self._make_speech_frame(500)
        for _ in range(5):
            buf.add_frame(speech)

        # Should auto-emit at max duration
        callback.assert_called_once()
        assert buf._state == VADState.IDLE

    def test_flush_returns_buffered_audio(self) -> None:
        cfg = VADConfig(energy_threshold=100)
        buf = UserAudioBuffer(user_id=42, config=cfg)

        speech = self._make_speech_frame(500)
        buf.add_frame(speech)
        buf.add_frame(speech)

        result = buf.flush()
        assert result is not None
        assert len(result) == 3840 * 2
        assert buf._state == VADState.IDLE

    def test_flush_returns_none_when_idle(self) -> None:
        buf = UserAudioBuffer(user_id=42)
        assert buf.flush() is None


# ---------------------------------------------------------------------------
# stt tests
# ---------------------------------------------------------------------------

class TestStereoToMono:
    """Tests for stereo-to-mono conversion."""

    def test_basic_conversion(self) -> None:
        # 2 stereo samples: (100, 200), (300, 400)
        stereo = struct.pack("<4h", 100, 200, 300, 400)
        mono = _stereo_to_mono(stereo)
        samples = struct.unpack(f"<{len(mono)//2}h", mono)
        assert samples == (150, 350)

    def test_empty_input(self) -> None:
        assert _stereo_to_mono(b"") == b""


class TestPcmToWav:
    """Tests for PCM-to-WAV conversion."""

    def test_produces_valid_wav(self) -> None:
        import wave
        import io

        pcm = struct.pack("<100h", *range(100))
        wav_data = _pcm_to_wav(pcm, sample_rate=16000, channels=1)

        # Verify it's a valid WAV
        buf = io.BytesIO(wav_data)
        with wave.open(buf, "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 16000
            assert wf.getnframes() == 100


class TestGroqWhisperSTT:
    """Tests for Groq Whisper STT provider."""

    def test_not_available_without_key(self) -> None:
        stt = GroqWhisperSTT(api_key="")
        assert not stt.is_available()

    def test_available_with_key(self) -> None:
        stt = GroqWhisperSTT(api_key="test-key")
        assert stt.is_available()

    def test_name(self) -> None:
        stt = GroqWhisperSTT(api_key="k", model="whisper-large-v3-turbo")
        assert stt.name == "groq_whisper:whisper-large-v3-turbo"

    @pytest.mark.asyncio
    async def test_short_audio_returns_empty(self) -> None:
        stt = GroqWhisperSTT(api_key="test-key")
        result = await stt.transcribe(b"\x00" * 100)  # Too short
        assert result == ""


# ---------------------------------------------------------------------------
# conversation tests
# ---------------------------------------------------------------------------

class TestVoiceConversationManager:
    """Tests for voice conversation LLM manager."""

    def test_history_management(self) -> None:
        mgr = VoiceConversationManager(max_history=3)
        messages = mgr._get_messages(user_id=1, user_text="hello")
        # System + 1 user message
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["content"] == "hello"

    def test_history_trimming(self) -> None:
        mgr = VoiceConversationManager(max_history=2)
        # Add 3 messages
        mgr._get_messages(1, "first")
        mgr._save_response(1, "response1")
        mgr._get_messages(1, "second")
        mgr._save_response(1, "response2")
        messages = mgr._get_messages(1, "third")

        # Should have system + last 2 history entries (trimmed to max_history)
        # max_history=2 means only 2 ConversationMessages kept
        history = mgr._histories[1]
        assert len(history) == 2

    def test_clear_history(self) -> None:
        mgr = VoiceConversationManager()
        mgr._get_messages(1, "hello")
        mgr._save_response(1, "hi")
        assert len(mgr._histories[1]) > 0

        mgr.clear_history(1)
        assert 1 not in mgr._histories

    @pytest.mark.asyncio
    async def test_generate_fallback_message(self) -> None:
        """With no API keys, should return fallback message."""
        mgr = VoiceConversationManager(
            groq_api_key="",
            cerebras_api_key="",
            ollama_base_url="http://localhost:99999",  # Won't connect
        )
        result = await mgr.generate(user_id=1, user_text="hello")
        assert "offline" in result.lower() or "sorry" in result.lower()
