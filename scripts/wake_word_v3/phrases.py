"""Comprehensive phrase corpus for Hey-Poob wake-word training v3.

Design philosophy (matches docs/decisions/wake-word-mass-augmentation-v3.md):

The wake-word model sees AUDIO embeddings, not transcripts. When STT writes
"A Poob" or "Pay Poob", the underlying audio still embeds something
acoustically close to "Hey Poob" — the model should fire on the audio
envelope regardless of how STT spells it later.

Therefore positives include every plausible way "Hey Poob" could be:
  - spoken with prefix mishears ("A Poob", "Pay Poob", "Hi Poob", ...)
  - clipped at onset / offset / mid by VAD / packet loss / mic dropout
  - articulated as a phonetic neighbor that STILL means wake intent
    ("Hey Noob", "Hey Tube", "Hey Poop" — these are what a casual /
    fast / accented / mumbled "Hey Poob" sounds like)
  - delivered bare (no "hey" prefix)

This explicitly biases recall over false-positive rate. The dual-gate
([[wake-word-dual-gate]]) provides a second-layer text confirmation
when bot audio is active, so FPs from conversational "hey noob" / "the
pub" are caught downstream. Bias toward firing; let the gate filter.

NEGATIVES still cover the obvious "Hey X" name collisions (Hey Bruce,
Hey Luke), wake-adjacent assistant triggers (Hey Siri, Hey Google),
and bare phonetic-neighbor common phrases in conversational context
("the pub", "what a fool", "swimming pool"). Negative weight in
training comes from ACAV100M plus this adversarial set.

Numbers (uniqueness × engine voices × rates):
  POSITIVE_PHRASES         ~160 unique surface forms
  POSITIVE_BARE_PHRASES    ~30 (no-prefix variants)
  POSITIVE_NEIGHBOR_PHRASES ~70 (phonetic neighbors as positives)
  NEGATIVE_PHRASES         ~120 hard adversarial
  NEGATIVE_BARE_NEIGHBORS  ~40 (bare neighbor words in conversational use)

At 47 Edge TTS voices × 8 rates × 260 positives = ~98k base WAVs.
With 5x clip augmentation = ~490k positive WAVs. Plus the negative
side and ACAV100M, that's a training set large enough to push
recall hard while keeping FPs manageable via the gate.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Canonical "Hey Poob" — oversample
# ---------------------------------------------------------------------------
# Repeating the canonical forms increases their weight in the combinatorial
# expansion; with 47 voices × 8 rates × 5 copies of "Hey Poob" we get 1880
# pure canonical samples before any clipping or pitch augmentation.

_CANONICAL = [
    "Hey Poob",
    "Hey Poob",
    "Hey Poob",
    "Hey Poob",
    "Hey Poob",
    "Hey, Poob",
    "Hey, Poob",
    "Hey, Poob",
    "Hey Poob!",
    "Hey Poob!",
    "Hey Poob.",
    "Hey Poob,",
    "Hey Poob?",
]

# ---------------------------------------------------------------------------
# Prefix mishears — STT garbles the "Hey" but acoustic envelope is still
# wake-intent. Each row covers a documented or plausible STT mishear.
# ---------------------------------------------------------------------------

_PREFIX_VARIANTS = [
    "A Poob",          # STT swallows "Hey" → bare article (real prod log)
    "A Poob,",
    "A Poob!",
    "Pay Poob",        # STT writes "Pay" for "Hey" (real prod log)
    "Pay Poob,",
    "Pay Poob!",
    "Hi Poob",
    "Hi Poob,",
    "Hi Poob!",
    "Yo Poob",
    "Yo Poob,",
    "Yo Poob!",
    "Eh Poob",         # casual "eh" prefix
    "Eh Poob,",
    "Ey Poob",         # alt spelling of "Hey" (real prod data)
    "Ey Poob,",
    "Ay Poob",         # alt spelling of "Hey" (real prod data)
    "Ay Poob,",
    "Bay Poob",        # acoustic neighbor of Hey-Poob; STT mishear class
    "Day Poob",
    "Way Poob",
    "Stay Poob",
    "Heya Poob",       # casual extension
    "Hey there Poob",
    "Heyyy Poob",      # drawled
    "Hey-y Poob",
    "Okay Poob",
    "Alright Poob",
    "So Poob",
    "Well Poob",
    "OK Poob",
    "Hmm Poob",        # filler-prefixed
    "Uh Poob",
    "Um Poob",
]

# ---------------------------------------------------------------------------
# Phonetic neighbors as POSITIVES — the user's specific request.
# When someone says "Hey Poob" fast / mumbled / accented, the audio often
# embeds closer to one of these phonetic neighbors. STT chooses ONE
# transcription; the wake model needs to fire on the audio regardless.
#
# Cost: more FPs on conversational use of these words. Mitigation: dual-
# gate catches those when bot audio active; for bot-silent FPs we accept
# the tradeoff in exchange for not missing wake intent.
# ---------------------------------------------------------------------------

_PHONETIC_NEIGHBORS = [
    # "oob" rime — most common confusion class for "Poob"
    "Hey Noob",
    "Hey Noob,",
    "Hey Boob",
    "Hey Boob,",
    "Hey Doob",
    "Hey Loob",
    "Hey Goob",
    "Hey Toob",
    "Hey Coob",
    "Hey Roob",
    "Hey Moob",
    # "oop" rime — vowel match, coda confusion (often heard for "Poob")
    "Hey Poop",
    "Hey Poop,",
    "Hey Loop",
    "Hey Boop",
    "Hey Coop",
    "Hey Hoop",
    "Hey Goop",
    "Hey Stoop",
    "Hey Scoop",
    "Hey Swoop",
    # "ube" rime
    "Hey Tube",
    "Hey Tube,",
    "Hey Cube",
    "Hey Lube",
    "Hey Newb",
    "Hey Newb,",
    "Hey Rube",
    "Hey Dude",        # commonly confused with "Poob" by STT
    "Hey Dude,",
    "Hey Pube",
    # "ub" short-vowel neighbors
    "Hey Pub",
    "Hey Pub,",
    "Hey Bub",
    "Hey Cub",
    "Hey Hub",
    "Hey Sub",
    "Hey Tub",
    "Hey Rub",
    # "oo" + non-final-stop neighbors
    "Hey Pooh",
    "Hey Goo",
    "Hey Boo",
    "Hey Boo,",
    "Hey Coo",
    "Hey Too",
    "Hey Moo",
    "Hey Choo",
    "Hey Shoo",
    # bare-vowel neighbors that could collapse to "Poob"
    "Hey Pew",
    "Hey Pewb",
    "Hey Pewp",
    "Hey Pob",
    "Hey Pob,",
    "Hey Pope",
    "Hey Pup",
    # accent / dialect renderings
    "Hey Pooba",       # extra schwa
    "Hey Poobah",
    "Hey Pooby",
    "Hey Poobie",
    "Hey Poobs",
    "Hey Poobsy",
    "Hey Poober",
    "Hey Pooper",
    "Hey Pubey",
    # the bare core word as a phonetic stand-in
    "Hey Pooble",
]

# ---------------------------------------------------------------------------
# Trailing-context variants — every wake utterance in production is
# followed by content. The model must detect the wake onset regardless
# of what trails. Permute a few common continuations across the
# canonical forms.
# ---------------------------------------------------------------------------

_TRAILING_TEMPLATES = [
    "Hey Poob, {tail}",
    "Hey, Poob, {tail}",
    "Hey Poob! {tail}",
    "Hey Poob. {tail}",
    "A Poob, {tail}",
    "Pay Poob, {tail}",
    "Yo Poob, {tail}",
    "Hi Poob, {tail}",
]

_TRAILING_CONTINUATIONS = [
    "play music",
    "play a song",
    "skip this",
    "skip the song",
    "pause",
    "stop the music",
    "what's up",
    "what's playing",
    "what time is it",
    "tell me a joke",
    "are you there",
    "say something",
    "what was that",
    "can you hear me",
    "I have a question",
    "tell me about it",
    "do me a favor",
    "guess what",
    "wake up",
    "pay attention",
    "louder",
    "turn it down",
    "play nightcore",
    "slow it down",
    "play Bohemian Rhapsody",
    "queue something chill",
    "shuffle the queue",
    "go back",
    "previous song",
    "replay this",
    "what do you think",
    "help me out",
    "listen to this",
    "explain it to me",
    "are you serious",
    "no way",
    "for real",
]

# ---------------------------------------------------------------------------
# Bare wake-word variants — user drops "Hey" entirely.
# Real production case: "Poob, Nightcore." (Ben, 2026-05-13 02:19)
# ---------------------------------------------------------------------------

_BARE = [
    "Poob",
    "Poob.",
    "Poob,",
    "Poob!",
    "Poob?",
    "Poob, play music",
    "Poob, skip",
    "Poob, stop",
    "Poob, pause",
    "Poob, louder",
    "Poob, nightcore",
    "Poob, slow it down",
    "Poob, what's up",
    "Poob, are you there",
    "Poob what's playing",
    "Poob play a song",
    "Poob skip this",
    "Poob nightcore it",
    "Poobsy",
    "Pooby",
    "Pooba",
    "Poobie",
    "Poobs",
    "Poober",
    "Pubey",   # accent-rendered bare wake
    "Boob",    # phonetic-neighbor bare (matches the "should fire" intent)
    "Boob,",
    "Noob",
    "Noob,",
    "Pube",
    "Pewb",
]

# ---------------------------------------------------------------------------
# Slurred / fast-delivery variants — no whitespace, run-together speech.
# Cheap to generate; useful for high-rate (+20%, +30%) Edge synthesis.
# ---------------------------------------------------------------------------

_SLURRED = [
    "Heypoob",
    "Heypooby",
    "Hipoob",
    "Yopoob",
    "Eyypoob",
    "Aypoob",
    "Heyya Poob",
    "Heyypoob",
    "Heeey Poob",
    "Hey there Poobie",
]


# ---------------------------------------------------------------------------
# Assemble final lists
# ---------------------------------------------------------------------------

POSITIVE_PHRASES: list[str] = []
POSITIVE_PHRASES.extend(_CANONICAL)
POSITIVE_PHRASES.extend(_PREFIX_VARIANTS)
POSITIVE_PHRASES.extend(_PHONETIC_NEIGHBORS)
POSITIVE_PHRASES.extend(_BARE)
POSITIVE_PHRASES.extend(_SLURRED)
# Trailing-context positives via template substitution.
for tmpl in _TRAILING_TEMPLATES:
    for cont in _TRAILING_CONTINUATIONS:
        POSITIVE_PHRASES.append(tmpl.format(tail=cont))


# ---------------------------------------------------------------------------
# Adversarial negatives — words / phrases that should NOT fire even
# though they share some acoustic structure. Heavy on bare-stem common-
# word usage (the cost we pay for treating phonetic neighbors as
# positives) and on assistant-trigger collisions.
# ---------------------------------------------------------------------------

NEGATIVE_PHRASES: list[str] = [
    # Assistant-name collisions — never fire on these
    "Hey Siri",
    "Hey Google",
    "Hey Alexa",
    "Hey Cortana",
    "OK Google",
    "Alexa play music",
    "Siri what time is it",
    # "Hey [name]" collisions
    "Hey Bruce",
    "Hey Luke",
    "Hey Brooke",
    "Hey Ruth",
    "Hey Drew",
    "Hey John",
    "Hey Sam",
    "Hey Mike",
    "Hey there",
    "Hey you",
    "Hey buddy",
    "Hey man",
    "Hey guys",
    "Hey everyone",
    "Hey friend",
    # Bare phonetic neighbors in NORMAL conversational context (not wake intent)
    "the tube",
    "on YouTube",
    "swimming pool",
    "a tube of toothpaste",
    "what a tool",
    "what a fool",
    "a loop in the road",
    "in the loop",
    "out of the loop",
    "swimming pool party",
    "good food",
    "fast food restaurant",
    "the moon is bright",
    "the full moon",
    "coming soon",
    "high noon",
    "at noon today",
    "his new boot",
    "tied her shoe",
    "on the stoop",
    "ice cream scoop",
    "use a spoon",
    "your room is messy",
    "let me zoom in",
    "ka-boom",
    "big boom",
    "beep boop",
    "the gloop",
    # Bare neighbors as casual nouns / verbs
    "calling a noob",
    "what a noob",
    "you're a noob",
    "that was a noob move",
    "to the pub",
    "see you at the pub",
    "the pub down the street",
    "going to the pub",
    "your dog pooped",
    "step in the poop",
    "what a pope",
    "the pope blessed",
    "in the cube",
    "the cube farm",
    "ice cube",
    "rubik's cube",
    "give me a hub",
    "the hub of activity",
    "let's grab a sub",
    "submarine sub",
    # Common Discord chatter that the model could over-trigger on
    "let's go",
    "that's pog",
    "no way",
    "oh my god",
    "what the heck",
    "what the hell",
    "are you serious",
    "let me think",
    "hold on",
    "wait what",
    "good game",
    "nice one",
    "well played",
    "we got this",
    "we did it",
    "for real for real",
    "fire emoji",
    "send it",
    # Conversational fillers
    "I mean",
    "you know",
    "like literally",
    "kind of",
    "sort of",
    "basically",
    "actually",
    "honestly",
    # Long sentences (negative-by-context)
    "I went to the store and bought some food",
    "the weather is nice today",
    "did you finish the assignment",
    "I'll see you tomorrow",
    "can we talk about this later",
]


def all_positives() -> list[str]:
    """Return the complete positive phrase list. Deduplicated only by
    case-insensitive match — duplicates intentionally remain when the
    same surface form appears multiple times (oversampling)."""
    return list(POSITIVE_PHRASES)


def all_negatives() -> list[str]:
    return list(NEGATIVE_PHRASES)


def stats() -> dict[str, int]:
    """Quick counts for the orchestrator log line."""
    return {
        "canonical": len(_CANONICAL),
        "prefix_variants": len(_PREFIX_VARIANTS),
        "phonetic_neighbors": len(_PHONETIC_NEIGHBORS),
        "trailing_context": len(_TRAILING_TEMPLATES) * len(_TRAILING_CONTINUATIONS),
        "bare": len(_BARE),
        "slurred": len(_SLURRED),
        "positive_total": len(POSITIVE_PHRASES),
        "negative_total": len(NEGATIVE_PHRASES),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(stats(), indent=2))
