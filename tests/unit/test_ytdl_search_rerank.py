"""Tests for AsyncYTDL.search candidate re-ranking.

2026-07-11 prod: "play copper and clay" queued a 31-minute pottery video
("Can Art Clay Copper be torch fired? Yep!") and an earlier variant queued a
2-hour drama compilation — take-first-result had no notion of song
plausibility. The re-rank keeps the best-guess-over-dead-end contract from
[[ytdl-search-best-guess-fallback]] (never returns None where take-first
returned a track) but picks the most song-plausible candidate by title-token
overlap + duration fit. See docs/decisions/music-search-candidate-rerank.md.

All network access is mocked at the ``extract_info`` boundary.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from poob.music.queue import Track
from poob.music.ytdl import AsyncYTDL


def _entry(
    title: str,
    duration: int | None = None,
    vid: str = "vid",
    live: bool = False,
) -> dict:
    """Minimal yt-dlp info-dict entry."""
    return {
        "title": title,
        "webpage_url": f"https://www.youtube.com/watch?v={vid}",
        "url": f"https://stream.example/{vid}",
        "duration": duration,
        "id": vid,
        "thumbnail": None,
        "extractor": "youtube",
        "is_live": live,
    }


def _search_info(*entries: dict) -> dict:
    return {"entries": list(entries)}


def _ytdl_with_passes(*passes: dict | None) -> AsyncYTDL:
    ytdl = AsyncYTDL()
    ytdl.extract_info = AsyncMock(side_effect=list(passes))  # type: ignore[method-assign]
    return ytdl


# ---------------------------------------------------------------------------
# search() behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plausible_first_hit_returned_without_widening() -> None:
    """A normal song-length, on-topic hit short-circuits — one network call."""
    ytdl = _ytdl_with_passes(_search_info(_entry("AJR - BANG! (Official Video)", 175)))

    track = await ytdl.search("bang bang bang a j r")

    assert track is not None
    assert "BANG" in track.title
    assert ytdl.extract_info.await_count == 1


@pytest.mark.asyncio
async def test_longform_offtopic_first_hit_triggers_rerank() -> None:
    """The exact prod failure: 31-minute pottery video for a song query.
    Widening surfaces a short on-topic candidate that outranks it."""
    pottery = _entry("Can Art Clay Copper be torch fired? Yep!", 1859, vid="pot")
    song = _entry("Copper & Clay (Official Audio)", 210, vid="song")
    ytdl = _ytdl_with_passes(
        _search_info(pottery),
        _search_info(pottery, song),
    )

    track = await ytdl.search("copper and clay")

    assert track is not None
    assert track.identifier == "song"
    assert ytdl.extract_info.await_count == 2
    widened_call = ytdl.extract_info.await_args_list[1].args[0]
    assert widened_call.startswith("ytsearch5:")


@pytest.mark.asyncio
async def test_zero_overlap_longform_hit_loses_to_any_short_match() -> None:
    """The 2-hour drama-compilation case: no title overlap + long-form.
    Any partially-matching song-length candidate wins."""
    drama = _entry("Unrelated 2-hour drama compilation", 7708, vid="drama")
    partial = _entry("Copper Kettle (live)", 240, vid="partial")
    ytdl = _ytdl_with_passes(
        _search_info(drama),
        _search_info(drama, partial),
    )

    track = await ytdl.search("copper and clay by nona cryl")

    assert track is not None
    assert track.identifier == "partial"


@pytest.mark.asyncio
async def test_longform_query_intent_disables_the_gate() -> None:
    """Users asking for a mix/playlist WANT long-form — no widening, the
    2-hour result plays as requested (2000s throwback mix, prod-observed)."""
    mix = _entry("2000's music hits ~throwback playlist ~2000s vibes mix", 7737)
    ytdl = _ytdl_with_passes(_search_info(mix))

    track = await ytdl.search("2000s throwback playlist mix")

    assert track is not None
    assert "2000" in track.title
    assert ytdl.extract_info.await_count == 1


@pytest.mark.asyncio
async def test_empty_first_pass_reranks_widened_candidates() -> None:
    """Best-guess fallback improved: the widened pass picks the BEST-scoring
    entry, not blindly the first one."""
    junk = _entry("Unrelated hour-long vlog", 3900, vid="junk")
    good = _entry("Mister Saxobeat (Official Video)", 194, vid="good")
    ytdl = _ytdl_with_passes(
        None,
        _search_info(junk, good),
    )

    track = await ytdl.search("mister saxobeat")

    assert track is not None
    assert track.identifier == "good"


@pytest.mark.asyncio
async def test_implausible_hit_survives_when_widening_finds_nothing() -> None:
    """The additive guarantee: re-ranking never dead-ends a query that
    previously returned a track. Widen empty → pass-1 hit still plays."""
    pottery = _entry("Can Art Clay Copper be torch fired? Yep!", 1859, vid="pot")
    ytdl = _ytdl_with_passes(_search_info(pottery), None)

    track = await ytdl.search("copper and clay")

    assert track is not None
    assert track.identifier == "pot"


@pytest.mark.asyncio
async def test_both_passes_empty_returns_none() -> None:
    ytdl = _ytdl_with_passes(None, None)
    assert await ytdl.search("nonexistent gibberish") is None


@pytest.mark.asyncio
async def test_direct_url_never_widens_even_when_longform() -> None:
    """A pasted URL is the source of truth — a 2-hour video the user linked
    plays as-is, no plausibility second-guessing."""
    long_video = _entry("Some 2 hour video", 7200, vid="lv")
    ytdl = _ytdl_with_passes({**long_video})

    track = await ytdl.search("https://www.youtube.com/watch?v=lv")

    assert track is not None
    assert track.identifier == "lv"
    assert ytdl.extract_info.await_count == 1


@pytest.mark.asyncio
async def test_direct_url_extract_failure_returns_none_without_widening() -> None:
    """Preserved from the original decision: URL extraction failure is
    final — a relaxed search would be the wrong song."""
    ytdl = _ytdl_with_passes(None)
    assert await ytdl.search("https://youtu.be/deadbeef") is None
    assert ytdl.extract_info.await_count == 1


# ---------------------------------------------------------------------------
# Scoring units
# ---------------------------------------------------------------------------


def _track(title: str, seconds: int | None, live: bool = False) -> Track:
    return Track(
        title=title,
        url="https://example.com/x",
        duration=timedelta(seconds=seconds) if seconds is not None else None,
        is_stream=live,
    )


def test_relevance_prefers_song_length_on_equal_overlap() -> None:
    q = "copper and clay"
    song = AsyncYTDL._relevance_score(q, _track("Copper & Clay", 200))
    longform = AsyncYTDL._relevance_score(q, _track("Copper and Clay pottery guide", 1900))
    assert song > longform


def test_relevance_prefers_overlap_over_none() -> None:
    q = "copper and clay"
    on_topic = AsyncYTDL._relevance_score(q, _track("Copper Clay Song", 200))
    off_topic = AsyncYTDL._relevance_score(q, _track("Totally unrelated", 200))
    assert on_topic > off_topic


def test_relevance_ignores_duration_for_longform_queries() -> None:
    q = "lofi study mix"
    long_mix = AsyncYTDL._relevance_score(q, _track("lofi study mix 2 hours", 7200))
    short = AsyncYTDL._relevance_score(q, _track("lofi study mix", 200))
    # No duration bonus/penalty either way — overlap dominates.
    assert long_mix >= short - 0.001


def test_implausible_gate_boundaries() -> None:
    ytdl = AsyncYTDL()
    assert not ytdl._is_implausible_hit("song name", _track("x", 899))
    assert ytdl._is_implausible_hit("song name", _track("x", 901))
    # Long-form intent disables the gate.
    assert not ytdl._is_implausible_hit("song name mix", _track("x", 7200))
    # URLs are exempt.
    assert not ytdl._is_implausible_hit("https://youtube.com/watch?v=x", _track("x", 7200))
    # Unknown duration → leave alone.
    assert not ytdl._is_implausible_hit("song name", _track("x", None))


# ---------------------------------------------------------------------------
# Junk-title guard — 2026-07-16/17 census: all three junk results were UNDER
# the 900s duration gate (Funko unboxing 11:55, ICE press conference 10:15,
# app-store tutorial 7:33). A junk marker now triggers the widened re-rank
# regardless of duration and penalizes the candidate in scoring; it never
# hard-rejects (best-guess-over-dead-end preserved).
# ---------------------------------------------------------------------------


def test_junk_marker_triggers_rerank_under_duration_gate() -> None:
    """The 'Bitty Funk' -> Funko unboxing case: 715s slips the duration gate,
    the 'opening every' marker must catch it."""
    ytdl = AsyncYTDL()
    funko = _track("Opening Every Set Of Funko Bitty Pops!", 715)
    assert ytdl._is_implausible_hit("bitty funk", funko) is True
    presser = _track("ICE/USCIS STEM OPT Press Conference", 615)
    assert ytdl._is_implausible_hit("opt", presser) is True
    tutorial = _track("5 Tricks To Get Your App Into the Top Charts (Apple App Store)", 453)
    assert ytdl._is_implausible_hit("app at top", tutorial) is True


def test_junk_marker_penalty_lets_music_win_the_rerank() -> None:
    """Even at full token overlap, a junk-marked candidate loses to a real
    song with partial overlap."""
    q = "bitty funk"
    junk = AsyncYTDL._relevance_score(q, _track("Opening Every Set Of Funko Bitty Pops!", 715))
    song = AsyncYTDL._relevance_score(q, _track("Bitty Funk (Official Audio)", 200))
    assert song > junk


def test_junk_markers_do_not_hit_real_song_titles() -> None:
    """The marker list must never penalize actual songs — the traps that
    forced the multi-word marker design."""
    for title in (
        "The Fray - How to Save a Life (Official Video)",
        "Diana Ross - Chain Reaction",
        "Trailer Park Boys Theme (cover)",
        "AJR - BANG! (Official Video)",
        "dumb dumb - mazie//Slowed and Reverb",
    ):
        assert AsyncYTDL._looks_like_junk_title(title) is False, title


@pytest.mark.asyncio
async def test_junk_pass1_widens_and_picks_real_song() -> None:
    """End-to-end: junk pass-1 hit under the duration gate -> widen -> the
    actual song wins."""
    junk = _entry("Opening Every Set Of Funko Bitty Pops!", 715, vid="junk")
    song = _entry("Bitty Funk (Official Audio)", 200, vid="song")
    ytdl = _ytdl_with_passes(
        _search_info(junk),
        _search_info(junk, song),
    )

    track = await ytdl.search("bitty funk")

    assert track is not None
    assert track.identifier == "song"


@pytest.mark.asyncio
async def test_junk_pass1_survives_when_widening_finds_nothing_better() -> None:
    """Additive guarantee holds for the junk trigger too: if the widened pool
    has nothing, the junk hit still plays rather than dead-ending."""
    junk = _entry("Opening Every Set Of Funko Bitty Pops!", 715, vid="junk")
    ytdl = _ytdl_with_passes(_search_info(junk), None)

    track = await ytdl.search("bitty funk")

    assert track is not None
    assert track.identifier == "junk"


# --- 2026-07-29 session: news/podcast junk under the duration gate ----------
# Two of six song queues that night were news content about a person in the
# headlines, both UNDER the 900s gate, and the user SKIPPED one of them:
#   "Diddy Heilett"  (STT garble) -> "Diddy Trial Explained: What you need to
#                                     know" [8:50 = 530s]
#   "Diddy"                       -> "Joe Rogan REVEALS Why The Inmates
#                                     Attacked Diddy In Prison" [12:19 = 739s]
# Neither hit an existing marker. Both are the sub-900s gap the 07-17 addendum
# was written for. Markers stay MULTI-WORD per that note's discipline: the
# shorter "you need to know" was rejected in testing because it swallows
# "Everything You Need To Know About Love".


def test_news_explainer_junk_markers_trigger_rerank() -> None:
    """The exact 2026-07-29 prod titles must be caught, both under the gate."""
    ytdl = AsyncYTDL()
    explainer = _track("Diddy Trial Explained: What you need to know", 530)
    assert ytdl._is_implausible_hit("diddy heilett", explainer) is True
    podcast = _track("Joe Rogan REVEALS Why The Inmates Attacked Diddy In Prison", 739)
    assert ytdl._is_implausible_hit("diddy", podcast) is True


def test_news_markers_do_not_swallow_real_song_titles() -> None:
    """False-positive guard. These are the adversarial cases that killed the
    shorter marker candidates during design."""
    ytdl = AsyncYTDL()
    for title, secs in (
        ("Everything You Need To Know About Love", 200),  # kills "you need to know"
        ("What You Need - Depeche Mode", 240),
        ("The Trial - Pink Floyd", 160),
        ("Why - Annie Lennox", 290),
        ("Diddy Heil Epstein - Official Music Video [REUPLOAD]", 203),
        ("P Diddy - I'll Be Missing You", 340),
        ("Revelations - Iron Maiden", 380),
    ):
        assert ytdl._is_implausible_hit("some song", _track(title, secs)) is False, title


def test_news_markers_are_multi_word_only() -> None:
    """Discipline guard from the 07-17 addendum: bare words are traps
    ('explained' alone would hit a song titled 'Explained'). Every marker
    must contain a space."""
    for marker in AsyncYTDL._JUNK_TITLE_MARKERS:
        assert " " in marker or marker in {
            "unboxing",
            "tutorial",
            "walkthrough",
            "keynote",
            "sermon",
            "briefing",
            "documentary",
            "audiobook",
        }, f"bare-word marker {marker!r} added without justification"
