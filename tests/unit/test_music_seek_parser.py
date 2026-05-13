"""Tests for the seek time-format parser.

The parser accepts the formats every other music bot supports plus the
voice-friendly relative forms:

  - Absolute mm:ss / m:ss / h:mm:ss   "2:30", "0:45", "1:02:03"
  - Absolute m-and-s         "2m30s", "1m", "45s", "2h", "1h5m30s"
  - Absolute bare seconds    "150", "37"
  - Relative                 "+10", "-5", "+10s", "-1m"

Returns ``ParsedSeek(seconds=float, relative=bool)``. ``relative=True``
means caller should add to ``position_seconds`` rather than seek to an
absolute offset. Negative absolute / invalid input raises
``SeekParseError`` with a message the handler can surface to the user.
"""

from __future__ import annotations

import pytest

from poob.music.seek import ParsedSeek, SeekParseError, parse_seek_input


# ---------------------------------------------------------------------------
# Absolute clock formats — "h:mm:ss" / "mm:ss" / "m:ss"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected_seconds", [
    ("0:00", 0.0),
    ("0:30", 30.0),
    ("1:00", 60.0),
    ("2:30", 150.0),
    ("10:45", 645.0),
    ("1:02:03", 3723.0),
    ("0:01:00", 60.0),
])
def test_parses_clock_format(raw: str, expected_seconds: float) -> None:
    p = parse_seek_input(raw)
    assert p == ParsedSeek(seconds=expected_seconds, relative=False)


def test_clock_format_rejects_seconds_above_59() -> None:
    """``2:60`` is ambiguous — most parsers reject. Voice STT also
    produces "2:60" mishears that should fail loud, not roll over."""
    with pytest.raises(SeekParseError):
        parse_seek_input("2:60")


def test_clock_format_rejects_minutes_above_59_in_h_mm_ss() -> None:
    with pytest.raises(SeekParseError):
        parse_seek_input("1:75:00")


# ---------------------------------------------------------------------------
# Suffixed m-and-s formats
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected_seconds", [
    ("45s", 45.0),
    ("2m", 120.0),
    ("2m30s", 150.0),
    ("1h", 3600.0),
    ("1h5m30s", 3930.0),
    ("0s", 0.0),
    ("90s", 90.0),       # > 59 is fine in this format
    ("1m30", 90.0),      # bare digits after `m` treated as seconds
])
def test_parses_suffixed_format(raw: str, expected_seconds: float) -> None:
    p = parse_seek_input(raw)
    assert p == ParsedSeek(seconds=expected_seconds, relative=False)


def test_suffixed_format_is_case_insensitive() -> None:
    """STT capitalizes unpredictably."""
    assert parse_seek_input("2M30S").seconds == 150.0
    assert parse_seek_input("1H5m").seconds == 3900.0


def test_suffixed_format_rejects_unknown_unit() -> None:
    with pytest.raises(SeekParseError):
        parse_seek_input("5x")


# ---------------------------------------------------------------------------
# Bare seconds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected_seconds", [
    ("0", 0.0),
    ("150", 150.0),
    ("37", 37.0),
    ("3600", 3600.0),
])
def test_parses_bare_seconds(raw: str, expected_seconds: float) -> None:
    p = parse_seek_input(raw)
    assert p == ParsedSeek(seconds=expected_seconds, relative=False)


# ---------------------------------------------------------------------------
# Relative formats — "+N" / "-N" / "+Ns" / "-Nm"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected_seconds", [
    ("+10", 10.0),
    ("-5", -5.0),
    ("+10s", 10.0),
    ("-1m", -60.0),
    ("+1m30s", 90.0),
    ("-30s", -30.0),
])
def test_parses_relative_format(raw: str, expected_seconds: float) -> None:
    p = parse_seek_input(raw)
    assert p == ParsedSeek(seconds=expected_seconds, relative=True)


def test_zero_relative_is_a_no_op_but_valid() -> None:
    """``+0`` is a defensible request (e.g. clear-and-replay-from-here).
    Parse it cleanly; don't reject."""
    p = parse_seek_input("+0")
    assert p == ParsedSeek(seconds=0.0, relative=True)


# ---------------------------------------------------------------------------
# Whitespace + edge cases
# ---------------------------------------------------------------------------

def test_strips_surrounding_whitespace() -> None:
    assert parse_seek_input("  2:30  ").seconds == 150.0
    assert parse_seek_input(" +10 ").relative is True


def test_empty_or_none_input_raises() -> None:
    with pytest.raises(SeekParseError):
        parse_seek_input("")
    with pytest.raises(SeekParseError):
        parse_seek_input("   ")


def test_garbage_input_raises_with_helpful_message() -> None:
    with pytest.raises(SeekParseError) as excinfo:
        parse_seek_input("the future")
    msg = str(excinfo.value).lower()
    # Error should hint at the accepted formats so the handler can
    # echo it back to the user.
    assert "2:30" in str(excinfo.value) or "format" in msg


def test_negative_absolute_seconds_rejected() -> None:
    """``-150`` is parsed as relative (-150s). The only way to express a
    negative absolute is to be malformed — we reject in the parser to
    keep callers from accidentally producing pre-zero seeks."""
    # -150 is a valid relative offset
    p = parse_seek_input("-150")
    assert p == ParsedSeek(seconds=-150.0, relative=True)
    # But "-2:30" (negative clock) is rejected — clock times are absolute
    # by convention; a negative absolute clock is nonsense.
    with pytest.raises(SeekParseError):
        parse_seek_input("-2:30")


def test_extreme_values_parse_within_reason() -> None:
    """24h+ tracks are rare but exist (lofi streams, podcasts).
    The parser shouldn't cap; let the player clamp at seek time."""
    p = parse_seek_input("25:00:00")
    assert p.seconds == 25 * 3600


def test_decimal_seconds_truncate_or_round_consistently() -> None:
    """`90.5s` is uncommon but legal; round to the nearest 0.1s."""
    p = parse_seek_input("90.5s")
    assert 90.4 < p.seconds < 90.6
