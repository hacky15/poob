"""Tests for Poob-noise filler generation.

Fillers are pre-generated once at startup and cached. They must be
synthesized via the supplied voice (Poob's Google Fenrir) so the
"thinking noise" sounds like Poob, cache-keyed by (voice, phrase) so a
voice/phrase change regenerates, and stale clips must be pruned (so old
word-fillers don't linger on the volume). See
docs/decisions/poob-noise-fillers.md.
"""

from __future__ import annotations

import pytest

import poob.voice.fillers as fillers
from poob.voice.fillers import FILLER_PHRASES, generate_fillers


class _FakeSynth:
    """Minimal TTSProvider double: records calls, returns deterministic bytes."""

    def __init__(self, name: str = "google_tts:en-US-Chirp3-HD-Fenrir") -> None:
        self._name = name
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    async def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        return b"MP3:" + text.encode()

    def is_available(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _tmp_filler_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(fillers, "FILLER_DIR", tmp_path)
    return tmp_path


def test_phrases_are_noises_not_words() -> None:
    # The whole point of the change: noises, not the old helpdesk words.
    joined = " ".join(FILLER_PHRASES).lower()
    assert "let me think" not in joined
    assert "good question" not in joined
    # Should contain Poob-style vocalizations.
    assert any("hmm" in p.lower() or "augh" in p.lower() or "mmm" in p.lower()
               for p in FILLER_PHRASES)


@pytest.mark.asyncio
async def test_generates_one_clip_per_phrase_via_synth(_tmp_filler_dir) -> None:
    synth = _FakeSynth()
    paths = await generate_fillers(synthesizer=synth)
    assert len(paths) == len(FILLER_PHRASES)
    assert set(synth.calls) == set(FILLER_PHRASES)  # each phrase synthesized
    for p in paths:
        assert p.exists() and p.read_bytes().startswith(b"MP3:")


@pytest.mark.asyncio
async def test_idempotent_skips_existing(_tmp_filler_dir) -> None:
    synth = _FakeSynth()
    await generate_fillers(synthesizer=synth)
    first = list(synth.calls)
    synth.calls.clear()
    await generate_fillers(synthesizer=synth)  # second run
    assert synth.calls == []  # nothing re-synthesized (all cached)
    assert first  # sanity: first run did work


@pytest.mark.asyncio
async def test_voice_change_regenerates_and_prunes(_tmp_filler_dir) -> None:
    await generate_fillers(synthesizer=_FakeSynth(name="google_tts:fenrir"))
    before = set(_tmp_filler_dir.glob("filler_*.mp3"))
    assert before
    # Different voice → different hash → new files, old ones pruned.
    new_synth = _FakeSynth(name="google_tts:enceladus")
    paths = await generate_fillers(synthesizer=new_synth)
    after = set(_tmp_filler_dir.glob("filler_*.mp3"))
    assert len(paths) == len(FILLER_PHRASES)
    # No stale clips from the old voice remain.
    assert after == set(paths)
    assert before.isdisjoint(after)


@pytest.mark.asyncio
async def test_falls_back_to_edge_when_synth_unavailable(_tmp_filler_dir, monkeypatch) -> None:
    # Synthesizer fails every clip → must fall back to Edge.
    class _DeadSynth:
        name = "google_tts:dead"

        async def synthesize(self, text: str) -> bytes:
            raise RuntimeError("google down")

    edge_calls: list[str] = []

    class _FakeEdge:
        def __init__(self, voice: str, rate: str) -> None:
            self.name = f"edge_tts:{voice}"

        async def synthesize(self, text: str) -> bytes:
            edge_calls.append(text)
            return b"EDGE:" + text.encode()

    import poob.voice.tts as tts
    monkeypatch.setattr(tts, "EdgeTTS", _FakeEdge)

    paths = await generate_fillers(synthesizer=_DeadSynth())
    assert set(edge_calls) == set(FILLER_PHRASES)  # edge produced every clip
    assert all(p.read_bytes().startswith(b"EDGE:") for p in paths)


@pytest.mark.asyncio
async def test_no_synth_and_no_edge_yields_no_clips(_tmp_filler_dir, monkeypatch) -> None:
    # No synthesizer and Edge import fails → graceful empty, no crash.
    import poob.voice.tts as tts

    def _boom(*a, **k):
        raise ImportError("no edge")

    monkeypatch.setattr(tts, "EdgeTTS", _boom)
    paths = await generate_fillers(synthesizer=None)
    assert paths == []
