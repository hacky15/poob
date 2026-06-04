"""Tests for the text wake-word / command-address matcher.

`_text_wake_word_match` is Layer 2 of wake detection (the text gate feeding
the dual-gate's `text_match`). It has two recognition paths:

  - "hey poob" + phonetic variants (the original, load-bearing path).
  - "command address": a poob-variant opening the utterance + an imperative
    verb, for transcripts where Deepgram dropped the "hey" lead-in.

Ground truth is the labeled production data in
docs/incidents/wake-gate-stt-mishear-rejection.md: real addresses with a
dropped "hey" MUST match; conversational mentions of "poob" and pure noise
MUST keep being rejected (the spurious-fire rejection is load-bearing —
~468 false fires in 5h of logs depend on it). See
docs/decisions/wake-word-dual-gate.md for why the stem stays narrow.
"""

from __future__ import annotations

import pytest

from poob.voice.dual_pipeline import DualPipelineProcessor


def _matcher() -> DualPipelineProcessor:
    """A processor instance for calling _text_wake_word_match.

    The regexes and the method live at class scope and touch no instance
    state, so __new__ (no Deepgram/wake init) is sufficient.
    """
    return DualPipelineProcessor.__new__(DualPipelineProcessor)


def _match(transcript: str) -> bool:
    return _matcher()._text_wake_word_match(transcript)


# ---------------------------------------------------------------------------
# Layer B — real addresses with the "hey" dropped/mangled by STT.
# Currently MISSED by the bare "hey poob" regex; the command-address path
# must recover them.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "transcript",
    [
        "A Poob play fighting music.",
        "Poob, nightcore,",
        "A Poob, a queue up panda by designer, Reverse, slow dank",
        "Apoob, remove nightcore.",
        "Poob, remove the night core.",
        "A Poob. Play try not to get scared.",
    ],
)
def test_dropped_hey_command_address_matches(transcript: str) -> None:
    assert _match(transcript) is True


# ---------------------------------------------------------------------------
# Layer A — clean "hey poob" addresses. Regression guard: these already
# worked via _TEXT_WAKE_RE and must keep matching.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "transcript",
    [
        "K. Ready up, Noah. Hey, Poob. Play be like a woman.",
        "Hey, Poob. Remove nightcore.",
        "hey, Poob, normal volume.",
        "Hey, Poob. Slow this song.",
    ],
)
def test_hey_poob_addresses_still_match(transcript: str) -> None:
    assert _match(transcript) is True


# ---------------------------------------------------------------------------
# Poob present but NOT an address — must keep being rejected. Either the
# poob-variant isn't at the opener, or there's no command verb.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "transcript",
    [
        "What? Fuck? That was fucked up, Poob.",            # poob at end
        "I think you took a little too long, Meta, between Poob. What",  # mid
        "two just by having Poob play an audio sound that they already",  # mid + verb
        "I'm just so yeah. This this would be in our PUBG playlist.",  # pub/play guarded
        "Poob has to talk to his side piece who loves chili roll.",       # opener but no verb
        "If I heard Poob talking, like,",                   # mid + no verb
    ],
)
def test_poob_mention_not_address_rejected(transcript: str) -> None:
    assert _match(transcript) is False


# ---------------------------------------------------------------------------
# Pure noise — no poob, no address — must keep being rejected.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "transcript",
    [
        "Bye bye bye.",
        "Andy's Canadian, so it's already minus five points.",
        "Are you talking about young Sheldon? Some YC or YS dude?",
        "Get Poob out of",  # poob present but mid-utterance + no verb
    ],
)
def test_noise_rejected(transcript: str) -> None:
    assert _match(transcript) is False


# ---------------------------------------------------------------------------
# Extra edge cases.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("poob skip", True),               # opener + verb
        ("poob turn it off", True),        # "turn" is a verb
        ("poob volume up", True),
        ("uh poob play this", True),       # filler word "uh"
        ("um, poob, skip", True),          # filler "um" + commas
        ("oh poob stop", True),
        ("apoob play it", True),           # glued filler "a"
        ("kpoob skip", True),              # glued filler "k"
        ("Poob shut up", False),           # opener but "shut"/"up" not verbs
        ("is poob broken", False),         # poob not at opener + no verb
        ("poob", False),                   # opener, no verb
        ("a poob", False),                 # filler + opener, no verb
        ("please play something", False),  # verb but no poob
        ("spoof play that song", False),   # "spoof" must not parse as s+poof
        ("scoob play", False),             # glued letter restricted to a/k
        ("the noob played well", False),   # "noob" not at opener + "played" != play
    ],
)
def test_edge_cases(transcript: str, expected: bool) -> None:
    assert _match(transcript) is expected
