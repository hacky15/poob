"""Time-format parser for the ``seek`` music action.

The parser accepts the formats every real music bot supports plus the
voice-friendly relative forms — `"2:30"`, `"2m30s"`, `"150"`, `"+10"`,
`"-1m"`. See ``docs/decisions/music-seek.md`` for the rationale and the
roadmap reference.

Returns ``ParsedSeek(seconds, relative)``. ``relative=True`` means the
caller should add ``seconds`` to the current position. ``relative=False``
means ``seconds`` is an absolute target (>= 0).

Why a dedicated parser rather than inline regex in the handler: the
parser is the natural-language surface for both the LLM tool args
(structured) and the voice STT-derived raw user text (free-form). Both
paths converge here. Voice STT can produce all sorts of capitalizations,
whitespace, and spurious punctuation; the parser absorbs that without
forcing the LLM to normalize.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class SeekParseError(ValueError):
    """Raised when input doesn't match any supported seek format."""


@dataclass(frozen=True, slots=True)
class ParsedSeek:
    """Result of parsing a user-supplied seek expression."""

    seconds: float
    relative: bool


# ---------------------------------------------------------------------------
# Sub-parsers — each one returns ``ParsedSeek`` or raises.
# ---------------------------------------------------------------------------

_CLOCK_RE = re.compile(
    r"^(?:(\d{1,3}):)?(\d{1,3}):(\d{1,2})$"
)
_SUFFIX_RE = re.compile(
    r"^"
    r"(?:(\d+(?:\.\d+)?)h)?"
    r"(?:(\d+(?:\.\d+)?)m)?"
    r"(?:(\d+(?:\.\d+)?)s?)?"
    r"$",
    re.IGNORECASE,
)
_BARE_SECONDS_RE = re.compile(r"^\d+(?:\.\d+)?$")
_RELATIVE_PREFIX_RE = re.compile(r"^([+\-])(.+)$")


def _parse_clock(raw: str) -> ParsedSeek | None:
    """``h:mm:ss`` / ``m:ss`` — colon-separated, always absolute."""
    m = _CLOCK_RE.match(raw)
    if not m:
        return None
    h_str, mid_str, s_str = m.group(1), m.group(2), m.group(3)
    seconds = int(s_str)
    if seconds > 59:
        raise SeekParseError(
            f"invalid clock format {raw!r}: seconds must be 0-59"
        )
    if h_str is not None:
        # h:mm:ss — middle group is minutes, must be 0-59
        minutes = int(mid_str)
        if minutes > 59:
            raise SeekParseError(
                f"invalid clock format {raw!r}: minutes must be 0-59"
            )
        total = int(h_str) * 3600 + minutes * 60 + seconds
    else:
        # m:ss — left group is minutes (no upper bound, e.g. "75:00")
        total = int(mid_str) * 60 + seconds
    return ParsedSeek(seconds=float(total), relative=False)


def _parse_suffixed(raw: str) -> ParsedSeek | None:
    """``1h5m30s`` / ``2m30s`` / ``2m30`` / ``45s``. Bare digits after a
    matched ``m`` are treated as seconds."""
    # Tolerate a trailing bare digit run after an `m` ("1m30" → 90s).
    # The base regex requires `s?` at the tail; allow it to capture a
    # bare digit group as the seconds component.
    m = _SUFFIX_RE.match(raw)
    if not m:
        return None
    h_str, m_str, s_str = m.group(1), m.group(2), m.group(3)
    if h_str is None and m_str is None and s_str is None:
        return None
    # Validate at least one component is present. Pure empty match is
    # filtered above; this guards against a stray future regex change.
    has_unit = any(part is not None for part in (h_str, m_str, s_str))
    if not has_unit:
        return None
    h = float(h_str) if h_str is not None else 0.0
    mm = float(m_str) if m_str is not None else 0.0
    ss = float(s_str) if s_str is not None else 0.0
    return ParsedSeek(seconds=h * 3600 + mm * 60 + ss, relative=False)


def _parse_bare_seconds(raw: str) -> ParsedSeek | None:
    """A bare integer / float — absolute seconds."""
    if not _BARE_SECONDS_RE.match(raw):
        return None
    return ParsedSeek(seconds=float(raw), relative=False)


def _parse_relative(raw: str) -> ParsedSeek | None:
    """``+10`` / ``-5s`` / ``+1m30s`` — relative offset.

    Strips the sign, parses the remainder as an absolute, then applies
    the sign. ``-2:30`` is rejected (clock format is conventionally
    absolute; a negative absolute clock is nonsense). Negative bare-
    seconds / negative suffix forms ARE permitted (they're how the
    user expresses "go back 30s").
    """
    m = _RELATIVE_PREFIX_RE.match(raw)
    if not m:
        return None
    sign_str, rest = m.group(1), m.group(2)
    sign = 1.0 if sign_str == "+" else -1.0

    # Disallow negative clock (h:mm:ss / m:ss).
    if ":" in rest and sign < 0:
        raise SeekParseError(
            f"invalid seek format {raw!r}: negative clock time not allowed"
        )

    # Try absolute sub-parsers on the unsigned remainder.
    for fn in (_parse_clock, _parse_suffixed, _parse_bare_seconds):
        result = fn(rest)
        if result is not None:
            return ParsedSeek(seconds=sign * result.seconds, relative=True)
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_seek_input(raw: object) -> ParsedSeek:
    """Parse a user-supplied seek expression. Raises ``SeekParseError``.

    Accepts the formats listed in this module's docstring. Whitespace
    around the expression is stripped; the parser is case-insensitive
    for the suffixed form.
    """
    if not isinstance(raw, str):
        raise SeekParseError(f"seek input must be a string, got {type(raw).__name__}")
    s = raw.strip()
    if not s:
        raise SeekParseError(
            "empty seek input; accepted formats: '2:30', '2m30s', '150', '+10', '-5'"
        )

    # Relative formats first — the leading sign disambiguates from the
    # other branches. Any sub-parser inside _parse_relative may raise
    # SeekParseError; let it propagate.
    rel = _parse_relative(s)
    if rel is not None:
        return rel

    # Absolute branches. Clock first because it has the most specific
    # shape; suffixed next; bare seconds last as a fallback.
    for fn in (_parse_clock, _parse_suffixed, _parse_bare_seconds):
        result = fn(s)
        if result is not None:
            return result

    raise SeekParseError(
        f"unrecognized seek format {raw!r}; "
        "accepted formats: '2:30', '2m30s', '150', '+10', '-5'"
    )
