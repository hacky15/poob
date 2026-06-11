"""Tests for Poob-noise filler generation.

Fillers are pre-generated once at startup and cached. Each phrase carries its
own ``(rate, weight)``: clips are synthesized via a rate-aware factory (Poob's
Google Fenrir) so the "thinking noise" sounds like Poob, cache-keyed by
``(voice, rate, phrase)`` so a voice/rate/phrase change regenerates, stale clips
are pruned, and the same phrase at two rates is two distinct clips. The player
selects weighted-randomly. See docs/decisions/poob-noise-fillers.md.
"""

from __future__ import annotations

import pytest

import poob.voice.fillers as fillers
from poob.voice.fillers import (
    FILLER_PHRASES,
    JOIN_PHRASES,
    FillerPlayer,
    generate_fillers,
    pick_join_phrase,
)


class _FakeSynth:
    """Minimal TTSProvider double: records calls, returns deterministic bytes."""

    def __init__(self, name: str = "google_tts:en-US-Chirp3-HD-Fenrir",
                 calls: list[str] | None = None) -> None:
        self._name = name
        self.calls = calls if calls is not None else []

    @property
    def name(self) -> str:
        return self._name

    async def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        return b"MP3:" + text.encode()

    def is_available(self) -> bool:
        return True


def _factory(name: str = "google_tts:en-US-Chirp3-HD-Fenrir"):
    """Return (synth_factory, shared_calls_list). The factory builds a synth
    per rate that all append to one shared call log."""
    calls: list[str] = []
    return (lambda rate: _FakeSynth(name=name, calls=calls)), calls


def _phrases() -> set[str]:
    return {p for p, _r, _w in FILLER_PHRASES}


@pytest.fixture(autouse=True)
def _tmp_filler_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(fillers, "FILLER_DIR", tmp_path)
    return tmp_path


def test_phrases_are_noises_not_words() -> None:
    phrases = _phrases()
    joined = " ".join(phrases).lower()
    # The old helpdesk word-fillers must not return.
    assert "let me think" not in joined
    assert "good question" not in joined
    # Approved vocalizations dominate (augh / ohh / ugh families). Pure "mmm"
    # fillers were rejected — none should be a bare mmm.
    assert any("augh" in p.lower() for p in phrases)
    assert not any(p.lower().strip(".").replace("m", "") == "" for p in phrases), \
        "a pure-mmm filler regressed (operator rejected those)"


def test_each_phrase_carries_rate_and_weight() -> None:
    for entry in FILLER_PHRASES:
        phrase, rate, weight = entry  # shape contract
        assert isinstance(phrase, str) and phrase
        assert 0.5 <= rate <= 1.5
        assert weight > 0


def test_hero_phrase_is_weighted_heaviest() -> None:
    """`Uuughhh.` is the 2026-06-10 re-workshop hero ("very very good") — it
    must carry the top weight."""
    by_weight = sorted(FILLER_PHRASES, key=lambda e: e[2], reverse=True)
    assert by_weight[0][0] == "Uuughhh."


def test_roster_is_all_slow_zone_no_speedup_hack() -> None:
    """The old roster forced 6 clips to rate 1.25 to dodge Fenrir's spell-out;
    the re-workshop proved slow spellings vocalize, so every clip must now sit
    in the drawn-out moan zone — no fast 1.25 entries (the hack is gone, not
    worked around)."""
    for phrase, rate, _w in FILLER_PHRASES:
        assert rate <= 0.85, f"{phrase!r} at rate {rate} — the 1.25 speed hack regressed"


def test_spell_out_phrases_kept_at_slow_rate() -> None:
    """Ughhh / Uuughhh are the exact clips the 1.25 hack existed for; the
    operator confirmed they vocalize at slow rate, so they're kept slow."""
    by_phrase = {p: r for p, r, _w in FILLER_PHRASES}
    assert by_phrase.get("Ughhh.") is not None and by_phrase["Ughhh."] <= 0.85
    assert by_phrase.get("Uuughhh.") is not None and by_phrase["Uuughhh."] <= 0.85


def test_filler_treatment_gentle_default_loud_rare() -> None:
    """Loudness: a gentle loudnorm is the normal sound (soft moans must NOT go
    through the speech speechnorm — that was the harshness bug); the loud
    treatment surfaces rarely (operator: 'B normal, C rare')."""
    from poob.voice.fillers import (
        FILLER_AF_GENTLE,
        FILLER_AF_LOUD,
        pick_filler_treatment,
    )

    assert "loudnorm" in FILLER_AF_GENTLE and "speechnorm" not in FILLER_AF_GENTLE
    assert "speechnorm" in FILLER_AF_LOUD

    counts = {"gentle": 0, "loud": 0}
    for _ in range(2000):
        af, _vol = pick_filler_treatment()
        counts["loud" if af == FILLER_AF_LOUD else "gentle"] += 1
    assert counts["gentle"] > counts["loud"] * 3   # gentle clearly dominates
    assert counts["loud"] > 0                        # but loud does surface for charm


@pytest.mark.asyncio
async def test_generates_one_clip_per_phrase_via_factory(_tmp_filler_dir) -> None:
    factory, calls = _factory()
    results = await generate_fillers(synth_factory=factory)
    assert len(results) == len(FILLER_PHRASES)
    assert set(calls) == _phrases()  # every phrase synthesized
    for path, weight in results:
        assert path.exists() and path.read_bytes().startswith(b"MP3:")
        assert weight > 0


@pytest.mark.asyncio
async def test_same_phrase_different_rate_are_distinct_clips(_tmp_filler_dir) -> None:
    """Two entries with the same phrase but different rate must produce two
    files (rate is in the cache key) — e.g. the kept `Aaaughhh.` at 0.7 and 0.8."""
    factory, _calls = _factory()
    phrases = [("Aaaughhh.", 0.7, 1.0), ("Aaaughhh.", 0.8, 1.0)]
    results = await generate_fillers(synth_factory=factory, phrases=phrases)
    paths = {p for p, _w in results}
    assert len(paths) == 2  # distinct files despite identical phrase text


@pytest.mark.asyncio
async def test_idempotent_skips_existing(_tmp_filler_dir) -> None:
    factory, calls = _factory()
    await generate_fillers(synth_factory=factory)
    assert calls
    calls.clear()
    await generate_fillers(synth_factory=factory)  # second run, all cached
    assert calls == []


@pytest.mark.asyncio
async def test_voice_change_regenerates_and_prunes(_tmp_filler_dir) -> None:
    f1, _ = _factory(name="google_tts:fenrir")
    await generate_fillers(synth_factory=f1)
    before = set(_tmp_filler_dir.glob("filler_*.mp3"))
    assert before
    f2, _ = _factory(name="google_tts:enceladus")
    results = await generate_fillers(synth_factory=f2)
    after = set(_tmp_filler_dir.glob("filler_*.mp3"))
    assert len(results) == len(FILLER_PHRASES)
    assert after == {p for p, _w in results}
    assert before.isdisjoint(after)  # old voice's clips pruned


@pytest.mark.asyncio
async def test_falls_back_to_edge_when_synth_unavailable(_tmp_filler_dir, monkeypatch) -> None:
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

    results = await generate_fillers(synth_factory=lambda rate: _DeadSynth())
    assert set(edge_calls) == _phrases()
    assert all(p.read_bytes().startswith(b"EDGE:") for p, _w in results)


@pytest.mark.asyncio
async def test_no_factory_and_no_edge_yields_no_clips(_tmp_filler_dir, monkeypatch) -> None:
    import poob.voice.tts as tts

    def _boom(*a, **k):
        raise ImportError("no edge")

    monkeypatch.setattr(tts, "EdgeTTS", _boom)
    results = await generate_fillers(synth_factory=None)
    assert results == []


# ---------------------------------------------------------------------------
# FillerPlayer — weighted selection + backward-compatible bare-Path input.
# ---------------------------------------------------------------------------


def test_player_accepts_bare_paths(tmp_path) -> None:
    """Legacy callers pass bare Paths (equal weight) — must still work."""
    clips = [tmp_path / "a.mp3", tmp_path / "b.mp3"]
    for c in clips:
        c.write_bytes(b"x")
    player = FillerPlayer(filler_paths=clips)
    assert player.available
    assert player.get_random_filler() in clips


def test_player_weighted_selection_favors_heavy(tmp_path, monkeypatch) -> None:
    """A heavily-weighted clip dominates random selection."""
    light = tmp_path / "light.mp3"
    heavy = tmp_path / "heavy.mp3"
    for c in (light, heavy):
        c.write_bytes(b"x")
    player = FillerPlayer(filler_paths=[(light, 1.0), (heavy, 99.0)])

    counts = {light: 0, heavy: 0}
    seq = iter([])  # force varied last_index by sampling many times

    for _ in range(400):
        # reset last_index occasionally so both stay eligible
        player._last_index = -1
        counts[player.get_random_filler()] += 1
    assert counts[heavy] > counts[light] * 5  # heavy clearly dominates


def test_player_avoids_immediate_repeat(tmp_path) -> None:
    a, b = tmp_path / "a.mp3", tmp_path / "b.mp3"
    for c in (a, b):
        c.write_bytes(b"x")
    player = FillerPlayer(filler_paths=[(a, 1.0), (b, 1.0)])
    first = player.get_random_filler()
    second = player.get_random_filler()
    assert first != second  # two-clip case never repeats consecutively


def test_player_empty_is_unavailable() -> None:
    player = FillerPlayer(filler_paths=[])
    assert not player.available
    assert player.get_random_filler() is None
    assert player.get_filler_bytes() is None


# ---------------------------------------------------------------------------
# JOIN_PHRASES — the weighted entrance "join noise" pool (cringe quip + moan).
# ---------------------------------------------------------------------------


def test_join_phrases_shape_and_weights() -> None:
    for phrase, weight in JOIN_PHRASES:
        assert isinstance(phrase, str) and phrase
        assert weight > 0
    by_weight = sorted(JOIN_PHRASES, key=lambda e: e[1], reverse=True)
    assert by_weight[0][0] == "Daddy's home, ohhh yeah."  # ~25%
    assert by_weight[1][0] == "Poob has arrived, aaaughhh."  # ~10%


def test_join_phrases_dropped_spelled_out_tails() -> None:
    """The tails that spelled out at 0.85 ('...rrraugh', '...ughhh') were
    replaced — they must not appear; the corrected phrases must."""
    joined = " ".join(p for p, _w in JOIN_PHRASES)
    assert "rrraugh" not in joined            # i06 tail fixed
    assert "Poob, ughhh" not in joined        # i09 tail fixed
    assert "Poob in the house, auugh." in {p for p, _w in JOIN_PHRASES}
    assert "It's ya boy Poob, ohhh." in {p for p, _w in JOIN_PHRASES}
    assert "Guess who? Poob! Aughh." in {p for p, _w in JOIN_PHRASES}  # i04 reworded


def test_pick_join_phrase_is_weighted(monkeypatch) -> None:
    counts: dict[str, int] = {}
    for _ in range(600):
        p = pick_join_phrase()
        counts[p] = counts.get(p, 0) + 1
    # The 25% phrase should clearly beat an ~8% phrase.
    assert counts.get("Daddy's home, ohhh yeah.", 0) > counts.get("Poob's back, ohhh.", 0)
