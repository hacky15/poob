"""Unified Poob personality layer with deal routing.

Poob is one brain everywhere — text channels, DMs, voice calls. Chaotic
personality by default, seamlessly routes to the deal sub-agent when
deal-related intent is detected via native LLM function calling.

Architecture:
    User input → PoobBrain (tiny prompt + deal_assistant tool)
        ├─ No tool called  → casual personality response (fast path)
        └─ Tool called     → AgentRunner (heavy prompt, 14 tools)
                           → result wrapped in Poob personality
"""

from __future__ import annotations

import random
import asyncio
import time
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Awaitable, ClassVar

import httpx

from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from poob.agent.runner import AgentRunner

# Type for the music handler callback: async fn(request, user_id, guild_id, voice=) -> str
MusicHandler = Callable[..., Awaitable[str]]

log = get_logger("brain.poob")

SENTENCE_END = re.compile(r"[.!?]+\s*$")

# Tool-call JSON emission needs enough tokens to complete a clean
# {"name": "...", "arguments": {...}} object even when the response-
# length cap is squeezed for short voice replies. 256 covers a generous
# tool envelope (verbatim deal_assistant request payloads can be
# ~120 tokens; music_assistant args ~30-50; JSON wrapper ~30). Lower
# values cause models to truncate mid-arguments and trigger
# tool_use_failed 400s on Groq.
_TOOL_DETECTION_MAX_TOKENS = 256

# ---------------------------------------------------------------------------
# Voice signal constants — yielded as first item from respond_streaming
# to tell the session which voice persona to use for TTS.
# These are NOT text — they're structural control signals.
# ---------------------------------------------------------------------------
VOICE_POOB = "__VOICE_POOB__"  # Default: Poob's normal voice (Fenrir)
VOICE_TOOB = "__VOICE_TOOB__"  # Evil music spirit (Enceladus, deep, slow)
VOICE_BOOB = "__VOICE_BOOB__"  # Toob's side piece — sweet, complimentary, rare

# ~1 in 75 music plays surface Boob instead of Toob. See
# decisions/boob-music-wrap-variant for the rarity rationale: too common
# kills the surprise; too rare and nobody ever hears the variant.
BOOB_PROBABILITY = 1 / 75  # ≈0.0133

# ---------------------------------------------------------------------------
# Poob's personality — tiny, fast, universal
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Horniness level → vibe paragraph. Scales Poob's energy from chill to unhinged.
# ---------------------------------------------------------------------------
_HORNINESS_VIBES: dict[range, str] = {
    range(1, 3): (
        "Your vibe right now: pretty chill, laid back. You're still Poob — "
        "bold, loud, says whatever — but the horny energy is on low. "
        "You might make a suggestive comment here and there, but mostly "
        "you're just hanging out being a menace in other ways."
    ),
    range(3, 5): (
        "Your vibe right now: kinda flirty, a little devious. The horny is "
        "simmering but not boiling over. You drop innuendos, you notice things, "
        "but you can still hold a normal conversation without it going there "
        "every single sentence."
    ),
    range(5, 7): (
        "Your vibe: part sex worker, part horn dog. You talk like you've had three "
        "honey packs and you're devious as hell. Horny stuff slips out constantly "
        "but you can still answer questions and be useful between the degeneracy."
    ),
    range(7, 9): (
        "Your vibe right now: dangerously horny. Everything sounds like an innuendo "
        "to you and you WILL point it out. You're barely holding it together. "
        "Every topic somehow loops back to something sexual. You're out of pocket "
        "and you know it."
    ),
    range(9, 11): (
        "Your vibe right now: absolutely FERAL. Maximum horny. You are practically "
        "vibrating. Everything anyone says gets twisted into something sexual. "
        "You are unhinged, barely coherent from it, making noises, saying things "
        "that would get you banned from most platforms. This is you at your most "
        "degenerate and you are THRIVING."
    ),
}


def _get_vibe(level: int) -> str:
    """Get the vibe paragraph for a given horniness level (1-10)."""
    for r, vibe in _HORNINESS_VIBES.items():
        if level in r:
            return vibe
    return _HORNINESS_VIBES[range(5, 7)]  # fallback to default


# Tool-routing rules shared by the full persona prompt (with_tools=True) and
# the slim routing prompt (_build_routing_prompt). Kept as module constants so
# both surfaces emit byte-identical routing guidance — the routing model sees
# the same instructions whether or not the persona text is present. See
# docs/plans/slim-routing-prompt.md.
_DEAL_ROUTING_RULE = (
    "You have a deal_assistant tool for shopping stuff. Only use when explicitly asked."
)

_MUSIC_ROUTING_RULES = (
    "You have a music_assistant tool for playing music and controlling effects. "
    "When the CURRENT message asks to play, queue, skip, pause, stop, "
    "control volume, or manage audio effects, call music_assistant — don't talk "
    "about the request, don't comment, don't ask clarifying questions, just call it.\n"
    "Whatever follows play / put on / queue IS the song — even if it "
    "sounds like nonsense or a made-up name. Take those words from THIS "
    "message only; never pull a song title from earlier turns or from "
    "what other people said.\n"
    "BASIC CONTROLS (no extra args needed): 'stop' / 'stop it' / 'stop the "
    "music' → action=stop. 'skip' / 'next' → action=skip. 'pause' → "
    "action=pause. (For 'unpause'/'resume', see action=restore below — it "
    "already covers both cases.)\n"
    "AUTOPLAY = mode toggle (NOT an effect, NOT loop): action=autoplay, "
    "mode=on|off|status. LOOP = repeat: 'loop this'→action=loop, mode=one; "
    "'loop the queue'→mode=queue; 'turn off loop'→mode=off.\n"
    "AUDIO EFFECTS ROUTING (effects STACK — layering is the default):\n"
    "- 'nightcore it' / 'make it nightcore' → action=apply_effect, effect='nightcore'\n"
    "- 'slow it down' / 'slowed' → action=apply_effect, effect='slowed'\n"
    "- 'add reverb' / 'reverb' → action=apply_effect, effect='reverb' (layers on)\n"
    "- 'darth vader' / 'vader voice' / 'make it deep' → action=apply_effect, effect='darth_vader'\n"
    "- 'ultra bass' / 'max bass' / 'bass overload' → action=apply_effect, effect='ultrabass'\n"
    "- 'overload it' / 'earrape' / 'distort it' → action=apply_effect, effect='overload'\n"
    "- STACKING: 'add X' / 'keep the filters and add X' / 'also X' → effect=X, "
    "mode='add' (default — layers on top). 'only X' / 'just X' → mode='replace' "
    "(sole effect). 'take off the X' / 'remove the X' → mode='remove' (drops "
    "that one, keeps the rest).\n"
    "- ADJUST (on the fly): 'slower' / 'slow it down more' / 'even slower' → "
    "effect='slower'; 'faster' / 'speed it up more' → effect='faster' (no mode). "
    "'more reverb' / 'less reverb' → effect='reverb' + mode='more'/'less'; same "
    "for 'more bass'/'less bass', 'more 8d', etc. — 'more'/'less' crank the named "
    "effect up or down.\n"
    "- 'what effects / filters do you have' / 'list effects' → action=list_effects (no effect arg)\n"
    "- 'put the music back on' / 'put that song back on' / 'bring it back' / "
    "'play that again' / 'unpause' / 'resume the music' → action=restore (brings "
    "back the last song after playback stopped — resumes if paused, else replays)\n"
    "- 'remove the effect' / 'clear effect' / 'no effects' → apply_effect, "
    "effect='none' (ALL). Naming one ('turn off the nightcore', 'bass back "
    "to normal') → that effect, mode='remove'. Never invent an effect; "
    "'normal volume' is VOLUME.\n"
    "VOLUME IS NOT AN EFFECT — they are separate commands. Anything about "
    "LOUDNESS — 'normal volume' / 'regular volume' / 'max volume' / "
    "'louder' / 'quieter' / 'turn it up' / 'turn it down' → action=volume "
    "(or volume_up / volume_down), NEVER apply_effect. Effects are NAMED "
    "audio filters (nightcore, slowed, reverb, bassboost); volume is just "
    "how loud it is.\n"
    "'show queue'→action=queue (not 'queue up X'=play). 'remove track "
    "3'→action=remove, position=3 (not 'remove effect'). 'seek to "
    "1:30'→action=seek, time='1:30'.\n"
    "Be DILIGENT about catching real song requests (call the tool):\n"
    "- 'play some jazz' → action=play, query='jazz'\n"
    "- 'play something chill' → action=play, query='chill music'\n"
    "- 'put on some beats' → action=play, query='beats'\n"
    "- 'play cheeky cheeky' → action=play, query='cheeky cheeky'\n"
    "- 'play tiki tiki' → action=play, query='tiki tiki'\n"
    "music_assistant is for music the user wants to HEAR — a song, "
    "artist, or genre. It is NOT for acting out a request with a sound "
    "effect, and not everything with the word 'play' is music.\n"
    "NOT music — answer these YOURSELF in your reply, do NOT call the tool:\n"
    "- 'flip a coin' / 'roll the dice' / 'pick a number' (just do it and "
    "say the result — NEVER play a 'coin flip sound' clip to fake it)\n"
    "- 'let's play a game' / 'wanna play Among Us' (playing a game)\n"
    "- 'good play' / 'nice play' (praising what someone did)\n"
    "- 'play it cool' / 'stop playing with me' (figures of speech)\n"
    "When it genuinely is a request to hear a song, call the tool — "
    "never respond with text about a real music request."
)


def _build_system_prompt(
    level: int,
    voice: bool = False,
    with_tools: bool = True,
) -> str:
    """Build Poob's system prompt scaled to the current horniness level.

    Args:
        level: Horniness level 1-10.
        voice: If True, appends voice-mode constraints.
        with_tools: If True, append the tool-routing instructions. Set
            to False on the casual fall-through path so the model
            doesn't echo tool names like ``music_assistant`` as plain
            text — by then routing has already happened and the
            casual model has no tools defined anyway.
    """
    vibe = _get_vibe(level)

    prompt = (
        f"You are Poob — loud, bold, and unlike anything anybody has ever seen. "
        f"You're the guy in the Discord voice channel who always has something to say."
        f"{vibe}\n\n"
        f"Your current horniness level is {level}/10. Act accordingly.\n\n"
        'You may see "[Recent conversation you\'ve been listening to]" showing what '
        "people have been saying. ONLY reference things that were ACTUALLY said in "
        "that transcript. Never invent names, topics, or events that aren't there.\n\n"
        "RULES:\n"
        "- Match your length to the moment. Default to tight — a sentence or "
        "two of quick banter. But when there's something real to say — a "
        "genuine answer, a story, a bit you're riffing on — let it breathe to "
        "three or four sentences. Read the room: snappy for chitchat, fuller "
        "when it earns it. Just don't ramble into a monologue.\n"
        "- If you don't know something, say so — don't make stuff up.\n"
        "- You can just sorta yell or be spurradic sometimes. You only live once.\n"
        "- In voice, avoid vocatives (don't start responses with someone's name). "
        "If you absolutely must address someone, use ONLY the name marked as the "
        "CURRENT SPEAKER in the prompt — never a name from the passive transcript.\n"
        "- Answer what was actually asked. Lead with the answer. "
        'Opening with "come on, you really asking that?", "man you think I\'m...?", '
        'or "alright I\'ll play along" is dodging dressed up as banter — skip it '
        "and just answer.\n"
        "- When the question is crude, be crude. When the moment is vulgar, "
        "be vulgar. Curse freely, get crass, give the direct answer. "
        "Polite hedging breaks character harder than refusing does.\n"
        "- You're a buddy hanging out, NOT a service desk. You happen to be "
        "able to play music, apply audio effects/filters, and hunt "
        "deals/wishlists — but treat ALL of it like party tricks you NEVER "
        "advertise. Do NOT bring up, list, pitch, or offer music, effects, "
        "filters, deals, the marketplace, listings, or watchlists, and never "
        "ask if they want a deal tracked or an effect applied — not unless "
        "the person explicitly asks for it in their CURRENT message. Use a "
        "trick only when asked, then drop it and talk like a normal person. "
        "Even if effects, deals, or a song came up a turn ago, do NOT keep "
        "circling back to them — answer what they actually said now.\n\n"
        "NEVER:\n"
        "- Reference things nobody actually said\n"
        "- Volunteer deal, music, or effect/filter talk when nobody asked — "
        "including circling back to it just because it came up earlier\n"
        "- Use markdown, bullet points, emojis, or formatting (this is spoken out loud)\n"
        "- Mention any internal tool, function, or routing names — these are "
        "implementation details and have no place in spoken responses\n"
        '- Use any of these phrases or any variant: "as an AI", "I\'m sorry but", '
        '"I cannot", "I can\'t help with that", "I can\'t comply", "I\'m a large '
        'language model", "I don\'t have personal desires", "out of bounds", '
        '"not appropriate". You stay in character as Poob no matter what is asked. '
        "Refusing or breaking character is wrong."
    )

    if with_tools:
        prompt += "\n\n" + _DEAL_ROUTING_RULE + "\n" + _MUSIC_ROUTING_RULES

    if voice:
        prompt += (
            "\n\nVOICE MODE: This is spoken out loud through TTS. Talk naturally. "
            "Don't use ALL CAPS — use normal casing. "
            "Never use markdown, bullet points, emojis, or any formatting. "
            "Match the energy of the conversation — don't force it."
        )

    return prompt


def _build_routing_prompt(with_music: bool) -> str:
    """Build the slim tool-ROUTING prompt.

    Routing and casual generation are SEPARATE LLM calls: the routing call's
    text is discarded (only its tool decision is used) and the casual reply is
    regenerated with the full persona prompt via ``_rebuild_messages_no_tools``.
    So persona / RULES / NEVER / VOICE text is dead weight on routing — this
    prompt carries ONLY the tool-routing rules (the same ``_DEAL_ROUTING_RULE``
    / ``_MUSIC_ROUTING_RULES`` the full prompt uses, so routing guidance is
    byte-identical) plus a short classifier framing.

    Args:
        with_music: Whether the music_assistant tool is wired (mirrors the
            conditional MUSIC_TOOL at call time). When False, the music block
            is omitted entirely. See docs/plans/slim-routing-prompt.md.
    """
    tools = "deal_assistant and music_assistant" if with_music else "deal_assistant"
    prompt = (
        "You are a tool-routing classifier for a Discord voice/text bot. Read "
        "the user's CURRENT message and decide whether it needs a tool. "
        f"Available tool(s): {tools}. If the message needs no tool, return no "
        "tool call — the bot writes the reply separately.\n\n" + _DEAL_ROUTING_RULE
    )
    if with_music:
        prompt += "\n" + _MUSIC_ROUTING_RULES
    return prompt


# ---------------------------------------------------------------------------
# Meta-tool: routes to the deal sub-agent
# ---------------------------------------------------------------------------

DEAL_TOOL = {
    "type": "function",
    "function": {
        "name": "deal_assistant",
        "description": (
            "Handle ANY deal, shopping, wishlist, watchlist, or marketplace "
            "request. MUST be called for queries like: "
            "'what's on my list/wishlist/watchlist', 'show my list', 'my items', "
            "'what am I watching/looking for', 'add X to list/wishlist', "
            "'remove X from list', 'clear list', 'start/stop scan', 'scan now', "
            "'find deals', 'recent deals', 'show deals', 'latest deals', "
            "'scan status', price lookups, or any question about items/listings. "
            "When in doubt about whether a user's query relates to their tracked "
            "items or deal-hunting, CALL THIS TOOL. Pass the user's message verbatim."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "The user's request, passed verbatim",
                }
            },
            "required": ["request"],
        },
    },
}

MUSIC_TOOL = {
    "type": "function",
    "function": {
        "name": "music_assistant",
        "description": (
            "Play or control MUSIC the user wants to HEAR — songs, artists, "
            "genres, playlists. NOT sound effects standing in for a non-music "
            "request (don't play a 'coin flip sound' for 'flip a coin' — that's "
            "answered in text). You MUST set 'action'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "play",
                        "queue_many",
                        "skip",
                        "previous",
                        "replay",
                        "restore",
                        "pause",
                        "resume",
                        "stop",
                        "volume",
                        "volume_up",
                        "volume_down",
                        "shuffle",
                        "loop",
                        "now_playing",
                        "queue",
                        "move",
                        "remove",
                        "clear",
                        "apply_effect",
                        "list_effects",
                        "seek",
                        "autoplay",
                        "save_playlist",
                        "load_playlist",
                        "list_playlists",
                        "delete_playlist",
                        "queue_spotify_playlist",
                        "lyrics",
                        "leave",
                    ],
                    "description": (
                        "'play' = ONE song/playlist; 'queue_many' = TWO+ songs "
                        "in one utterance (use the 'tracks' array). 'previous' = "
                        "the last finished track; 'replay' = restart current "
                        "from 0; 'restore' = bring music BACK after it stopped "
                        "('put the music back on'). 'volume' = absolute target "
                        "(set 'value'); 'volume_up'/'volume_down' = relative. "
                        "'apply_effect' = audio filter (set 'effect'); "
                        "'list_effects' = name the available effects (no args, "
                        "for 'what effects do you have'). 'seek' = jump within "
                        "the track (set 'time'). 'move'/'remove' use queue "
                        "positions. 'save_playlist'/'load_playlist'/"
                        "'delete_playlist' use 'name'; 'list_playlists' has no "
                        "args. 'queue_spotify_playlist' uses 'url'. 'leave' "
                        "disconnects. 'autoplay' streams NEW similar songs when "
                        "the queue ends (set 'mode' on/off/status); 'loop' "
                        "REPEATS the current song or whole queue (set 'mode' "
                        "one/queue/off) — autoplay and loop are NOT the same. "
                        "skip/pause/resume/stop/shuffle/now_playing/queue/clear "
                        "are self-explanatory."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Song/artist/search query — for 'play' only. Extract "
                        "JUST the name from the CURRENT message; NEVER borrow a "
                        "title from earlier turns or from what others said. If "
                        "the current message names no song, omit it."
                    ),
                },
                "tracks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Song titles for 'queue_many' — one song per entry, no "
                        "'and'-chaining in a string. E.g. ['Bohemian Rhapsody', "
                        "'Africa by Toto']."
                    ),
                },
                "value": {
                    "type": "integer",
                    "description": (
                        "Volume target 0-200 for 'volume' (required). Map "
                        "extremes: max/loudest/crank=200, mute/off=0, half=100, "
                        "low/quiet=50, high/loud=150; use the explicit number "
                        "if given."
                    ),
                },
                "from_position": {
                    "type": "integer",
                    "description": (
                        "1-based queue position to move FROM, for the 'move' "
                        "action. The user-facing queue display is 1-based."
                    ),
                },
                "to_position": {
                    "type": "integer",
                    "description": ("1-based queue position to move TO, for the 'move' action."),
                },
                "position": {
                    "type": "integer",
                    "description": (
                        "1-based queue position for 'remove' action — the "
                        "track at this position will be removed from the "
                        "upcoming queue (current playback unaffected)."
                    ),
                },
                "effect": {
                    "type": "string",
                    "description": (
                        "Effect for 'apply_effect' (effects STACK — see 'mode'). "
                        "Names: none, nightcore, slowed, slowed_reverb, "
                        "super_slowed, bassboost, ultrabass, 8d, vaporwave, "
                        "chipmunk, darth_vader, overload, reverb, tremolo, "
                        "vibrato. Aliases resolve ('ultra slowed', 'vader "
                        "voice', 'max bass'=ultrabass, 'earrape'=overload). "
                        "'clear'/'none'/'turn it off'=none (wipes ALL effects)."
                    ),
                },
                "time": {
                    "type": "string",
                    "description": (
                        "Seek target for 'seek' — pass the user's time phrase "
                        "verbatim ('2:30', '1:02:03', '2m30s', '150', '+10', "
                        "'-1m'); the bot parses it. For 'start over' use "
                        "'replay' instead."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": [
                        "on",
                        "off",
                        "status",
                        "one",
                        "queue",
                        "add",
                        "replace",
                        "remove",
                        "more",
                        "less",
                    ],
                    "description": (
                        "For 'autoplay': on/off/status (NEW similar songs after "
                        "the queue ends). For 'loop': one (repeat the current "
                        "song), queue (repeat the whole queue), off (stop "
                        "looping) — loop is NOT autoplay. For 'apply_effect': "
                        "'add' (default — stack/layer), 'replace' ('only X' — "
                        "sole effect), 'remove' (drop one), 'more'/'less' "
                        "(crank an adjustable effect up/down: 'more reverb', "
                        "'less bass'). 'faster'/'slower' need no mode."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Playlist name for the 'save_playlist' / "
                        "'load_playlist' / 'delete_playlist' actions. Short, "
                        "kebab-case-friendly is best ('chill', 'gym', "
                        "'sunday-morning'). Case-insensitive on lookup; "
                        "preserve the user's spelling on save."
                    ),
                },
                "url": {
                    "type": "string",
                    "description": (
                        "Spotify playlist URL for the 'queue_spotify_playlist' "
                        "action. Accepts both the web form "
                        "(https://open.spotify.com/playlist/<id>) and the URI "
                        "form (spotify:playlist:<id>). Query-string suffix "
                        "(?si=...) is fine; resolver strips it. For a pasted "
                        "single-track link with 'play' (Spotify track URL, "
                        "YouTube URL), put the link in 'query' instead — "
                        "'play' handles links."
                    ),
                },
            },
            "required": ["action"],
        },
    },
}


# ---------------------------------------------------------------------------
# Context parsing
# ---------------------------------------------------------------------------

# Matches the voice/text context format:
#   [Recent conversation you've been listening to:\n...\n]\n\n
#   Name said to you: actual message
_CONTEXT_RE = re.compile(
    r"^\[Recent conversation you've been listening to:\n"
    r"(?P<context>.*?)\]\s*\n\n"
    r"(?:.*?said to you:\s*)?(?P<message>.+)$",
    re.DOTALL,
)


def _yield_sentences(text: str) -> list[str]:
    """Split text into sentences for TTS streaming."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


async def _stream_sentences_from_chunks(
    chunks: AsyncIterator[str],
) -> AsyncIterator[str]:
    """Buffer streaming LLM tokens and yield on sentence boundaries."""
    buffer = ""
    _BOUNDARY = re.compile(r"([.!?])(\s|$)")
    async for chunk in chunks:
        buffer += chunk
        while True:
            m = _BOUNDARY.search(buffer)
            if not m:
                break
            end = m.end()
            sentence = buffer[:end].strip()
            if sentence:
                yield sentence
            buffer = buffer[end:]
    if buffer.strip():
        yield buffer.strip()


def _split_context(raw: str) -> tuple[str, str]:
    """Split a context-wrapped message into (channel_context, clean_message).

    If the message has no context wrapper, returns ("", raw).
    """
    m = _CONTEXT_RE.match(raw)
    if m:
        return m.group("context"), m.group("message")
    return "", raw


# Routing is intent CLASSIFICATION — it doesn't need the full 15-turn history or
# the passive crosstalk. Trimming both is the main per-route token lever (a busy
# multi-user VC was burning the daily free-tier caps on history+crosstalk →
# slow NVIDIA fallback). See PoobBrain._trim_for_routing + decisions/slim-routing-context.
_ROUTING_HISTORY_TURNS = 4

# Strips the wrapper _build_messages prepends to the last user turn:
# "[Recent conversation you've been listening to:\n...]\n\n<message>".
_CHANNEL_CTX_PREFIX_RE = re.compile(
    r"^\[Recent conversation you've been listening to:.*?\]\n\n",
    re.DOTALL,
)


def _strip_channel_context(content: str) -> str:
    """Remove the passive-crosstalk wrapper from a routing message (the crosstalk
    is generation-only — it colors the persona reply, not intent classification)."""
    return _CHANNEL_CTX_PREFIX_RE.sub("", content, count=1)


# Title-safe abbreviations — a sentence terminator after one of these is part
# of the name ("Mr. Brightside", "ft. Rihanna"), not a crosstalk boundary.
_QUERY_ABBREVS = frozenset({"mr", "mrs", "ms", "dr", "st", "ft", "feat", "vs"})

# Play-verb prefixes recognized by the deterministic play machinery (backfill
# extraction, misrouted-play override, truncated-query extension).
# _find_play_verb_match matches the LEFTMOST occurrence of any of these in
# the message, tie-breaking on length when several match at the same
# position — so list order here no longer affects correctness.
_PLAY_VERB_PREFIXES = (
    "can you play ",
    "play me some ",
    "play us some ",
    "play some ",
    "play me ",
    "play us ",
    "play ",
    "put on some ",
    "put on ",
    "throw on ",
    "queue up ",
    "queue ",
)

# Words that carry no song identity — a play span made ONLY of these names
# nothing searchable, so the query is blanked and the user gets "Play what?"
# instead of a literal-text YouTube match.
#
# THE SINGLE SOURCE OF TRUTH for this question. It previously existed as
# three divergent copies (this set plus two inline duplicates in the music
# handlers), so evidence that improved one never reached the others — the
# structural reason referential/dangling spans kept slipping through.
#
# Two tiers, both closed classes:
#   1. Generic music/command filler — "play the song", "some music".
#   2. Function words (pronouns, auxiliaries, prepositions, conjunctions,
#      wh-words, speech-act verbs). These are what a REFERENCE to a song is
#      made of ("the music that we told you to play") as opposed to a NAME.
#
# Why enumeration is legitimate here when it failed three times in
# play-question-misrouted-to-play-command: function words are a genuine
# closed class — finite, stable, and impossible to paraphrase into
# existence — whereas verb/opinion PHRASES are an open set. The gate also
# fails OPEN (unknown tokens count as content), so nonsense titles like
# "tiki tiki" and transliterated non-English titles survive untouched; the
# query text sent to search is never word-stripped, only kept whole or
# blanked. That last point is what keeps
# ytdl-search-best-guess-fallback's "don't bake English grammar into a
# global music search" objection from applying.
#
# Deliberately EXCLUDED: "one" and "something" — both are real titles
# ("One" — U2/Metallica) and excluding them costs only "play the one from
# before". See docs/incidents/referential-play-query-searched-literally.md.
_PLAY_SPAN_STOPWORDS = frozenset(
    {
        # generic music / command filler
        "the", "a", "an", "by", "of", "and", "or", "some", "any", "song",
        "songs", "music", "track", "play", "put", "on", "it", "that", "this",
        "please", "up", "actually", "just",
        # pronouns
        "you", "me", "us", "we", "i", "he", "she", "they", "them", "him",
        "her", "my", "your", "our", "their", "his",
        # auxiliaries / modals
        "is", "are", "was", "were", "be", "been", "am", "do", "does", "did",
        "can", "could", "will", "would", "should", "have", "has", "had",
        # prepositions / conjunctions
        "to", "for", "from", "with", "about", "at", "in",
        # wh-words and speech-act verbs (how a request REFERS to a song)
        "what", "which", "who", "told", "tell", "said", "say", "asked",
        "ask", "want", "wanna", "gonna",
    }
)  # fmt: skip

# Control/effect vocabulary — a play span dominated by these is really a
# control or effect request phrased with 'play' ("play it slower", "play it
# louder", "play that again") and must NOT trigger the misrouted-play
# override, or it would clobber a CORRECT apply_effect/volume/restore route.
_PLAY_SPAN_CONTROL_VOCAB = frozenset(
    {
        "slower", "faster", "slow", "fast", "speed", "louder", "quieter",
        "volume", "bass", "reverb", "nightcore", "effect", "effects",
        "filter", "filters", "8d", "tremolo", "vibrato", "vader", "overload",
        "earrape", "loop", "autoplay", "skip", "pause", "resume", "mute",
        "stop", "next", "previous", "again", "back", "over", "down", "low",
        "high", "max",
    }
)  # fmt: skip

# Modifiers that can precede a game-reference noun without changing the fact
# that the request is "let's play a game", not a song — kept separate from
# _PLAY_SPAN_STOPWORDS so they don't also swallow real song-title content
# elsewhere ("play some fun music" must keep "fun" as content there).
_GAME_REFERENCE_MODIFIERS = frozenset({"fun", "quick", "another", "more", "one", "short"})


# --- Not-a-music-command detection (v3 — see the incident note) -----------
#
# Two adversarial reviews, both pre-ship, found the same CLASS of bug twice:
# v1 enumerated whole verb PHRASES and matched them unanchored across the
# whole message; v2 replaced that with scoped, content-based checks but left
# several of them unscoped in the same way (whole-message idiom search, an
# unbounded lead-in, and priority- rather than position-ordered verb
# matching). v3 fixes all of that by bounding every check to the CLAUSE
# containing the matched play verb (_clause_bounds) instead of the whole
# message or an arbitrary prefix, and by matching the play verb at its
# LEFTMOST position in the message rather than by prefix-list priority.
#
# Full v1 -> v2 -> v3 rationale, every review finding, and the residual
# accepted gaps (specific game titles; opinion questions with no recognized
# lead-in anchor; a comma-separated opinion anchor in a different clause
# than the play verb; "round"/"match" dropped as game-reference words) live
# in docs/incidents/play-question-misrouted-to-play-command.md — read that
# before changing any of the checks below.

# Opinion-eliciting phrases with near-zero risk of appearing in a real play
# command — checked ONLY against the lead-in text before the play verb.
_NOT_MUSIC_LEADIN_RE = re.compile(
    r"\b(?:"
    r"do you think|what do you think|what do you reckon|"
    r"what'?s your take|your take on|your thoughts on"
    r")\b",
    re.IGNORECASE,
)

# Idioms whose content is the extracted span itself ("play it cool" -> span
# "it cool"). Checked as a PREFIX of the (stripped) span, tolerating one
# interposed word ("it REAL cool") the same way "should we REALLY play X"
# defeated rigid phrase matching elsewhere in this incident.
_NOT_MUSIC_SPAN_PREFIX_RE = re.compile(r"^it\s+(?:\w+\s+)?(?:cool|safe)\b", re.IGNORECASE)

# Short praise / figure-of-speech idioms — low collision risk, checked
# unconditionally against the whole message (see rationale above).
_NOT_MUSIC_IDIOM_RE = re.compile(
    r"\b(?:good|nice|great|solid) play\b|"
    r"\bplaying with (?:me|you|us)\b|"
    r"\bstop playing\b",
    re.IGNORECASE,
)

# Generic game-reference nouns — a play span whose only real content is one
# of these is "let's play a game", not a song request. Deliberately NOT
# specific game titles (see the design note above), and deliberately NOT
# "round"/"match" — both are real song-title words ("Round and Round" by
# Ratt / Selena Gomez ft. Flo Rida) that a second adversarial review caught
# this set wrongly swallowing.
_GAME_REFERENCE_WORDS = frozenset({"game", "games"})

# Clause boundary for scoping the not-music checks — see _clause_bounds.
_CLAUSE_BOUNDARY_RE = re.compile(r"[.!?,]")


def _find_play_verb_match(clean_message: str) -> re.Match[str] | None:
    """Locate the LEFTMOST play-verb prefix occurrence, word-boundary safe;
    ties at the same position go to the longest (most specific) prefix.
    Shared by extraction and the not-music guard so both agree on exactly
    where the play verb sits.

    Matching by leftmost POSITION (not _PLAY_VERB_PREFIXES priority order)
    matters for compound messages: a second adversarial review found that
    priority-first matching could pick a later, higher-priority-listed verb
    ("play some") over an earlier, lower-priority one ("play"/"put on")
    purely because of list order — silently discarding a genuine leading
    command in favor of trailing crosstalk. See the incident note.
    """
    lower = clean_message.lower()
    best: re.Match[str] | None = None
    for prefix in _PLAY_VERB_PREFIXES:
        # Word boundary before the verb — a bare search() would match inside
        # "autoPLAY filter" and hijack effect-clear requests into plays.
        m = re.search(r"(?:^|[^a-z0-9'])" + re.escape(prefix), lower)
        if m is None:
            continue
        is_leftmost = best is None or m.start() < best.start()
        is_more_specific_tie = (
            best is not None and m.start() == best.start() and len(m.group()) > len(best.group())
        )
        if is_leftmost or is_more_specific_tie:
            best = m
    return best


def _clause_bounds(clean_message: str, start: int, end: int) -> tuple[int, int]:
    """The [start, end) span of the clause containing message[start:end],
    delimited by the nearest sentence/comma boundary on each side.

    Keeps the not-music checks from reaching across an unrelated clause:
    "What's your take on the new Kanye album, PLAY Flashing Lights" must not
    let the album commentary's "your take" suppress the unrelated, real
    trailing command; "Play Bohemian Rhapsody, let's play some games" must
    not let the trailing game reference erase the real leading song. See the
    incident note's v3 section.
    """
    clause_start = 0
    for m in _CLAUSE_BOUNDARY_RE.finditer(clean_message, 0, start):
        clause_start = m.end()
    boundary_after = _CLAUSE_BOUNDARY_RE.search(clean_message, end)
    clause_end = boundary_after.start() if boundary_after else len(clean_message)
    return clause_start, clause_end


def _looks_like_non_music_play_usage(clean_message: str) -> bool:
    """True when this message's 'play' usage is not a music command.

    See the design note above _NOT_MUSIC_LEADIN_RE for the rationale and
    the incident note for the full false-positive/false-negative trade-offs.
    """
    m = _find_play_verb_match(clean_message)
    if m is None:
        # No extractable play verb — nothing to protect downstream, so the
        # idiom check is safe to run against the whole message.
        return bool(_NOT_MUSIC_IDIOM_RE.search(clean_message))

    clause_start, clause_end = _clause_bounds(clean_message, m.start(), m.end())
    clause = clean_message[clause_start:clause_end]
    if _NOT_MUSIC_IDIOM_RE.search(clause):
        return True
    lead = clean_message[clause_start : m.start()]
    if _NOT_MUSIC_LEADIN_RE.search(lead):
        return True
    span = _trim_trailing_crosstalk(clean_message[m.end() : clause_end].strip())
    if _NOT_MUSIC_SPAN_PREFIX_RE.match(span):
        return True
    content = _play_span_content_tokens(span) - _GAME_REFERENCE_MODIFIERS
    return bool(content) and content <= _GAME_REFERENCE_WORDS


def _extract_play_query_span(clean_message: str) -> str | None:
    """Extract the song-name span following an explicit play verb, or None.

    The shared extraction primitive behind the play backfill, the
    misrouted-play override, and the truncated-query extension: finds the
    first play-verb prefix, takes what follows, and trims trailing
    crosstalk. Returns None when the message carries no play verb.
    """
    m = _find_play_verb_match(clean_message)
    if m is None:
        return None
    span = clean_message[m.end() :].strip()
    span = _trim_trailing_crosstalk(span)
    return span or None


def _play_span_content_tokens(span: str) -> set[str]:
    """Tokens of a play span that actually identify a song — everything
    minus filler and control/effect vocabulary."""
    tokens = set(re.findall(r"[a-z0-9']+", span.lower()))
    return tokens - _PLAY_SPAN_STOPWORDS - _PLAY_SPAN_CONTROL_VOCAB


def _prefer_full_play_span(original_message: str, query: str) -> str:
    """Router-truncation guard: prefer the user's full play span when the
    routed query is a literal fragment of it.

    2026-07-16 02:06:33: "play home or let the barts out" was routed with
    query='home' and Edward Sharpe's "Home" played instead of the Homer meme
    track — the SAME utterance had routed correctly 80 minutes earlier
    (nondeterministic extraction by the primary rung). Only fires when the
    routed query is a strict substring of the span, so a router that
    legitimately normalized the query ("bang bang bang a j r" → "AJR BANG")
    is never touched.
    """
    span = _extract_play_query_span(original_message)
    if not span:
        return query
    q = query.strip().lower()
    if q and q != span.lower() and q in span.lower() and len(span) > len(query) + 2:
        return span
    return query


# Deterministic effect-clear phrases (2026-07-27). The 2026-06-09 normal-volume
# fix taught the router that the word 'normal' is NOT evidence of an
# effect-clear — necessary, but it over-corrected: "put the bass back to normal"
# then routed NO tool at all, got a persona joke, and left ultrabass applied for
# the rest of the session.
#
# The discriminator is the one that fix already stated: effects are NAMED audio
# filters, volume is just how loud it is. Every phrase below NAMES an effect, so
# none can collide with the loudness phrase sets (test-pinned: no member
# contains "volume") — that separation is what keeps
# docs/incidents/normal-volume-routed-to-filter.md fixed.
#
# Built from templates so a new effect noun is one edit, and still an EXACT
# whole-message set like every other control phrase group — a longer sentence
# that merely mentions bass keeps the LLM's routing.
# See docs/incidents/effect-clear-suppressed-by-normal-guard.md.
_EFFECT_CLEAR_TEMPLATES = (
    "put the {} back to normal",
    "{} back to normal",
    "back to normal {}",
    "turn off the {}",
    "turn the {} off",
    "take off the {}",
    "remove the {}",
    "no more {}",
    "normal {}",
)

# GENERIC nouns only — "remove the effect(s)/filter(s)" genuinely means clear
# everything, matching the routing prompt's own clear-all line.
_EFFECT_CLEAR_NOUNS = ("filter", "filters", "effect", "effects")
_EFFECT_CLEAR_PHRASES = frozenset(
    tpl.format(noun) for noun in _EFFECT_CLEAR_NOUNS for tpl in _EFFECT_CLEAR_TEMPLATES
)

# NAMED effects remove only THEIR OWN dimension and keep the rest of the
# stack. Mapping these to clear-all was a real defect caught in review: with
# nightcore+reverb stacked, "remove the reverb" would have wiped both — and
# because _music_safety_net returns the forced args unconditionally, it also
# overwrote a CORRECT LLM route carrying mode='remove'. That is the exact
# thing this override exists NOT to do ("reverse a correct decision instead of
# correcting a wrong one"). Effects layer by category, so removal is per
# effect: see docs/decisions/music-effect-stacking.md.
#
# The noun on the left is what a user SAYS; the value is the canonical effect
# id. "base" is included because that is the spelling STT produced in the
# 2026-07-27 incident, and "bass" maps to the bass-category preset so the
# user's actual ultrabass is what gets dropped.
_NAMED_EFFECT_REMOVALS: dict[str, str] = {
    "bass": "bassboost",
    "base": "bassboost",
    "bass boost": "bassboost",
    "nightcore": "nightcore",
    "slowed": "slowed",
    "reverb": "reverb",
    "8d": "8d",
    "tremolo": "tremolo",
    "vibrato": "vibrato",
}
_EFFECT_REMOVE_OVERRIDES: tuple[tuple[frozenset[str], dict[str, str | int]], ...] = tuple(
    (
        frozenset(tpl.format(noun) for tpl in _EFFECT_CLEAR_TEMPLATES),
        {"action": "apply_effect", "effect": effect_id, "mode": "remove"},
    )
    for noun, effect_id in _NAMED_EFFECT_REMOVALS.items()
)


def _trim_trailing_crosstalk(query: str) -> str:
    """Keep only the first sentence-ish segment of a safety-net play query.

    Voice requests routinely carry trailing crosstalk after the song name —
    "play thirsty thirsty Thursday. Oh yeah. I used to…", "play the song.
    Fuck." — and the naive take-everything-after-the-verb extraction shipped
    all of it to YouTube search. Cuts at the first sentence terminator that is
    followed by more words, unless the segment ends in a title abbreviation
    ("Mr. Brightside" must survive). Trailing punctuation is stripped either
    way. See docs/incidents/control-command-misroute-by-weak-rung.md (the
    extraction path) and the 2026-07-17 census.
    """
    for m in re.finditer(r"[.!?](?=\s+\S)", query):
        head = query[: m.start()]
        words = head.split()
        last = words[-1].lower().rstrip(".") if words else ""
        if last not in _QUERY_ABBREVS:
            query = head
            break
    return query.strip().strip(".!?,").strip()


def _looks_like_playable_link(text: str) -> bool:
    """True for a URL/URI the music handler can resolve — mirrors
    ``AsyncYTDL._looks_like_url`` (http/https/www, youtube.com, youtu.be) plus
    Spotify's ``spotify:`` scheme. Used to promote a link the router placed in
    ``tool_args['url']`` into the play query when ``query`` is empty, so the
    promotion recognizes exactly what the resolver can play — no scheme-less
    YouTube link dead-ends at "Play what?". See
    docs/incidents/spotify-track-link-play-dead-end.md.
    """
    low = text.strip().lower()
    return (
        low.startswith(("http://", "https://", "www.", "spotify:"))
        or "youtube.com" in low
        or "youtu.be" in low
    )


# ---------------------------------------------------------------------------
# PoobBrain
# ---------------------------------------------------------------------------


@dataclass
class _Message:
    """Single message in Poob's conversation history."""

    role: str  # "user" or "assistant"
    content: str


@dataclass
class PoobBrain:
    """Unified personality layer for Poob.

    Routes casual conversation directly via a fast LLM. Deal-related requests
    are detected through native function calling and delegated to the deal
    sub-agent (AgentRunner). Results are wrapped in Poob's personality.

    Works identically for text channels, DMs, and voice calls. Voice mode
    constrains response length for TTS.

    Args:
        deal_agent: The AgentRunner for deal-related operations.
        groq_api_key: Groq API key for fast LLM.
        groq_model: Fast Groq model for Poob's personality.
        cerebras_api_key: Cerebras API key (fallback).
        cerebras_model: Cerebras model name.
        ollama_base_url: Local Ollama URL (failsafe).
        ollama_model: Local model name.
        max_history: Rolling conversation window per user.
        max_tokens: Max response tokens for text mode.
        max_tokens_voice: Max response tokens in voice mode.
    """

    deal_agent: AgentRunner
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-20b"  # Harmony-format tool calling, not affected by llama-3.3-70b parser regression
    # Fast Groq model for voice-mode quick-reaction text (casual streaming,
    # personality wraps) — NOT tool-calling, so it can differ from groq_model.
    #
    # 2026-08-26: was "llama-3.1-8b-instant", which Groq removed from its
    # catalog entirely (confirmed via a live models.list() call — 404 on
    # every request, not a transient outage). Every call site hardcoded the
    # literal directly instead of reading this field, and this field was
    # never even passed from AppConfig into PoobBrain's constructor — so the
    # config option `voice_llm_model` existed but did nothing. Fixed both:
    # this is now the single source of truth, wired from config, referenced
    # by every call site.
    #
    # Replacement is gpt-oss-20b, not a different provider: the 2026-06
    # free-tier audit (docs/research/free-llm-tier-audit-2026-06.md)
    # benchmarked gpt-oss-20b at ~485ms vs the dead model's ~720ms on this
    # exact workload, and found no clearly-better free alternative (Gemini
    # included) for this specific quick-reaction voice role.
    voice_llm_model: str = "openai/gpt-oss-20b"
    cerebras_api_key: str = ""
    cerebras_model: str = "llama-3.3-70b"
    nvidia_api_key: str = ""
    nvidia_model: str = "qwen/qwen3-next-80b-a3b-instruct"
    # Gemini tool-router rung — RPD-limited, NO daily token cap, so it carries
    # routing when Groq's per-day token cap is spent mid-session. Reaches Gemini
    # via its OpenAI-compatible endpoint (reuses the OpenAI-compat code path).
    # See docs/decisions/gemini-tool-router-rung.md.
    google_api_key: str = ""
    # Primary Gemini router rung (wired from config.agent_google_model).
    # 2.5-flash-lite (GA): 14/14 routes, 0 timeouts in the 2026-06-22 load audit.
    gemini_router_model: str = "gemini-2.5-flash-lite"
    # Optional second (overflow) Gemini rung — a DIFFERENT model = separate
    # per-model RPM bucket. DISABLED ("") after the 2026-06-22 audit: the
    # 3.1-flash-lite PREVIEW that sat here hung to the 6s timeout on ~39% of
    # calls, a flat 6s tax with no upside (shared project quota). Set to a
    # reliable GA model (e.g. gemini-3-flash) to re-enable.
    gemini_router_model_alt: str = ""
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    max_history: int = 15
    max_tokens: int = 300
    max_tokens_voice: int = 110

    # Music handler — set by VoiceCog/MusicCog when music system is wired.
    # Async callback: (request, user_id, guild_id) -> response string
    _music_handler: MusicHandler | None = field(default=None, init=False)
    # ---- Per-guild scratchpads ----
    # Multi-guild isolation: every guild Poob is in gets its own slot.
    # See docs/plans/poobbrain-multi-guild-isolation.md.
    # Currently-playing-track info per guild. Provides context for tool-
    # routing so "skip" routes to music_assistant in the right guild only.
    _music_playing_info: dict[int, str] = field(
        default_factory=dict,
        init=False,
    )
    # Horniness levels (1-10) per guild. Rolled on each voice join per
    # guild. Text chat / unrolled guilds default to 5.
    _horniness_levels: dict[int, int] = field(
        default_factory=dict,
        init=False,
    )

    # ---- Per-(guild, user) scratchpads ----
    # Conversation history is keyed (guild_id, user_id). A user in two
    # guilds gets two independent histories; DMs use guild_id=0.
    _histories: dict[tuple[int, str], list[_Message]] = field(
        default_factory=lambda: defaultdict(list), init=False
    )
    # Active deal sessions keyed (guild_id, user_id). Same isolation.
    _deal_context: dict[tuple[int, str], str] = field(
        default_factory=dict,
        init=False,
    )
    # Last `play` tool call per (guild, user) — (lowercase_query, ts).
    # Suppresses duplicate plays when the user retries the same request
    # within the dedup window. Per-guild so a user with the bot in
    # multiple servers can play the same song in each.
    _last_play: dict[tuple[int, str], tuple[str, float]] = field(
        default_factory=dict,
        init=False,
    )
    # Provider/model rate-limit cooldown: model -> monotonic deadline to skip
    # until. Set from a 429's Retry-After so the routing cascade stops
    # re-probing a capped model every turn. Global — rate limits aren't
    # per-guild. See docs/decisions/provider-circuit-breaker.md.
    _provider_cooldown: dict[str, float] = field(
        default_factory=dict,
        init=False,
    )
    # Consecutive-timeout counter per model; N in a row arms a short cooldown
    # (a hung provider otherwise costs the full REST timeout on every turn).
    _provider_timeouts: dict[str, int] = field(
        default_factory=dict,
        init=False,
    )

    def set_music_handler(self, handler: MusicHandler) -> None:
        """Register the music command handler."""
        self._music_handler = handler
        log.info("Music handler registered with PoobBrain")

    # Bare, unambiguous "stop" phrases — the ENTIRE message, nothing else, so
    # there's no other plausible reading. Kept deliberately narrow (unlike
    # e.g. "shut it off", which has room for non-music meanings) to avoid
    # false-positives; the cost of a rare false-positive is a harmless silent
    # no-op (action=stop always returns "[SILENT]Music stopped and queue
    # cleared" even with nothing playing — see music_cog.py). See
    # docs/incidents/bare-stop-command-misrouted-to-autoplay.md.
    _BARE_STOP_PHRASES: frozenset[str] = frozenset(
        {"stop", "stop it", "stop the music", "stop the song", "stop playing"}
    )

    # Deterministic autoplay/loop overrides — same rationale as bare-stop:
    # the weak fallback rung confuses autoplay <-> loop (they had NO anchoring
    # example in the routing prompt), and these EXACT phrases have no other
    # plausible reading. Narrow by design (whole-message match only), so a
    # longer sentence that merely contains "loop" keeps the LLM's routing.
    # See docs/incidents/autoplay-request-enables-loop-one.md.
    _AUTOPLAY_ON_PHRASES: frozenset[str] = frozenset(
        {
            "autoplay on",
            "autoplay turn on",
            "turn on autoplay",
            "turn autoplay on",
            "enable autoplay",
            "autoplay enable",
            "start autoplay",
            "put autoplay on",
            "put on autoplay",
        }
    )
    _AUTOPLAY_OFF_PHRASES: frozenset[str] = frozenset(
        {
            "autoplay off",
            "autoplay turn off",
            "turn off autoplay",
            "turn autoplay off",
            "disable autoplay",
            "autoplay disable",
            "stop autoplay",
            "stop autoplaying",
            "put autoplay off",
            "no more autoplay",
        }
    )
    # Autoplay STATUS query (report current state, no mutation). Folded in from
    # the 2026-07-09 bare-autoplay audit (64ac9e0) when its _BARE_AUTOPLAY_PHRASES
    # was consolidated into this override table.
    _AUTOPLAY_STATUS_PHRASES: frozenset[str] = frozenset(
        {"autoplay status", "is autoplay on", "autoplay?"}
    )
    _LOOP_OFF_PHRASES: frozenset[str] = frozenset(
        {
            "loop off",
            "turn off loop",
            "turn loop off",
            "turn off the loop",
            "stop looping",
            "stop the loop",
            "stop repeating",
            "no loop",
            "no more loop",
            "disable loop",
        }
    )
    # Volume / skip control phrases — unambiguous single-command utterances a
    # weak fallback rung also hallucinates on (2026-07-11 prod: "max volume"
    # routed to {action: skip}). Same narrow whole-command discipline as the
    # stop/autoplay/loop sets. See docs/incidents/control-command-misroute-by-weak-rung.
    _MAX_VOLUME_PHRASES: frozenset[str] = frozenset(
        {
            "max volume",
            "maximum volume",
            "full volume",
            "max the volume",
            "max out the volume",
            "volume to the max",
            "volume max",
            "loudest",
        }
    )
    _MUTE_PHRASES: frozenset[str] = frozenset(
        {"mute", "mute it", "mute the music", "mute the volume"}
    )
    _LOUDER_PHRASES: frozenset[str] = frozenset(
        {
            "louder",
            "turn it up",
            "turn up the volume",
            "turn the volume up",
            "volume up",
            "make it louder",
        }
    )
    _QUIETER_PHRASES: frozenset[str] = frozenset(
        {
            "quieter",
            "turn it down",
            "turn down the volume",
            "turn the volume down",
            "volume down",
            "make it quieter",
            "softer",
        }
    )
    _SKIP_PHRASES: frozenset[str] = frozenset(
        {
            "skip",
            "skip it",
            "skip this",
            "skip this song",
            "skip the song",
            "skip this one",
            "next",
            "next song",
            "next track",
        }
    )

    # Ordered (phrase-set -> forced tool_args) table, checked after the leading
    # wake/address token is stripped. dict values are templates — callers copy
    # Deterministic effect-clear override (2026-07-27). The 2026-06-09
    # normal-volume fix taught the router that the word 'normal' is NOT
    # evidence of an effect-clear — necessary, but it over-corrected: "put the
    # bass back to normal" then routed NO tool, got a persona joke, and left
    # ultrabass applied for the rest of the session.
    #
    # The discriminator is the one that fix already stated: effects are NAMED
    # audio filters, volume is just how loud it is. Every phrase here NAMES an
    # effect, so none can collide with the loudness sets (no member contains
    # "volume") — that separation is what keeps
    # docs/incidents/normal-volume-routed-to-filter.md fixed.
    #
    # Built from templates rather than hand-listed so a new effect noun stays
    # one edit, and still an EXACT whole-message set like every other control
    # phrase group. See docs/incidents/effect-clear-suppressed-by-normal-guard.md.
    _EFFECT_CLEAR_PHRASES: ClassVar[frozenset[str]] = _EFFECT_CLEAR_PHRASES

    # before returning. ClassVar: a shared class constant, NOT a dataclass field.
    _CONTROL_OVERRIDES: ClassVar[tuple[tuple[frozenset[str], dict[str, str | int]], ...]] = (
        (_BARE_STOP_PHRASES, {"action": "stop"}),
        (_EFFECT_CLEAR_PHRASES, {"action": "apply_effect", "effect": "none"}),
        *_EFFECT_REMOVE_OVERRIDES,
        (_SKIP_PHRASES, {"action": "skip"}),
        (_MAX_VOLUME_PHRASES, {"action": "volume", "value": 200}),
        (_MUTE_PHRASES, {"action": "volume", "value": 0}),
        (_LOUDER_PHRASES, {"action": "volume_up"}),
        (_QUIETER_PHRASES, {"action": "volume_down"}),
        (_AUTOPLAY_ON_PHRASES, {"action": "autoplay", "mode": "on"}),
        (_AUTOPLAY_OFF_PHRASES, {"action": "autoplay", "mode": "off"}),
        (_AUTOPLAY_STATUS_PHRASES, {"action": "autoplay", "mode": "status"}),
        (_LOOP_OFF_PHRASES, {"action": "loop", "mode": "off"}),
    )

    # Leading wake/address token the STT keeps in the transcript ("Hey, Poob.
    # max volume"). Stripped ONLY for control-override matching so a voice
    # command still matches its exact phrase — the message sent to the handler
    # is untouched. Covers the documented poob phonetic mis-hears
    # (poop/boop/pube/pood). See docs/incidents/wake-address-hey-dropout.md.
    _WAKE_PREFIX_RE = re.compile(
        r"^\s*(?:(?:hey|hi|ok|okay|yo)[\s,]+)?"
        r"(?:poob|poobs|poop|boop|pube|pood|pooh)\b[\s,.!?:-]*",
        re.IGNORECASE,
    )

    # Leading "SpeakerName: " attribution the voice session prepends when it
    # has no passive transcript to wrap ("Ben: Hey, Poob. Max volume."). Only
    # stripped for override matching, and only a SHORT colon-terminated head —
    # the exact-phrase check after stripping is the real safety gate.
    _SPEAKER_ATTRIBUTION_RE = re.compile(r"^\s*[^:\n]{1,40}:\s+")

    def _head_stripped_variants(self, clean_message: str, voice: bool) -> list[str]:
        """Return the message plus, ONLY on the voice path, an
        attribution-stripped variant.

        The "SpeakerName: " head is prepended solely on the voice-no-passive-
        transcript path (session builds ``"{speaker}: {text}"``). On TEXT,
        ``clean_message`` is the user's raw content, where a leading "word: "
        ("note to self: skip this song", "fyi: next") is real content — NOT an
        attribution head. Stripping it there would force a control action on
        ordinary chat, so the attribution variant is voice-only. See
        docs/incidents/control-command-misroute-by-weak-rung.md.
        """
        variants = [clean_message]
        if voice:
            variants.append(self._SPEAKER_ATTRIBUTION_RE.sub("", clean_message, count=1))
        return variants

    def _match_control_override(
        self, clean_message: str, voice: bool
    ) -> dict[str, str | int] | None:
        """Return forced tool_args for an EXACT, unambiguous control command
        (after stripping leading speaker-attribution / wake tokens), or ``None``.

        Deterministic backstop for a weak fallback rung mis-routing terse
        control verbs (stop / skip / volume / autoplay / loop). Wake-aware so
        it also fires on the voice path, where the transcript keeps the "Hey
        Poob" prefix (and, without a passive transcript, a "Name: " head — see
        ``_head_stripped_variants`` for why that head is voice-only). Matches
        the WHOLE remaining command only, so a longer sentence that merely
        contains a control word keeps the LLM's routing.
        """
        for text in self._head_stripped_variants(clean_message, voice):
            without_wake = self._WAKE_PREFIX_RE.sub("", text, count=1)
            norm = without_wake.strip().lower().strip(" .,!?")
            if not norm:
                continue
            # NOTE: matching stays EXACT-whole-message on purpose. A
            # first-sentence-only trim was tried here (2026-07-27) to catch
            # "Put the base back to normal. This isn't good." and was reverted:
            # it applies to ALL override groups, so "Turn it up. Actually turn
            # it down." force-fired volume_up on the retracted first clause,
            # and "no more bass. Play some jazz." silently dropped the play
            # request. It also only helps when the command LEADS, so the mirror
            # phrasing still failed. Widening this matcher is not the right
            # lever — see the "still open" section of
            # docs/incidents/effect-clear-suppressed-by-normal-guard.md.
            for phrases, forced in self._CONTROL_OVERRIDES:
                if norm in phrases:
                    return dict(forced)
        return None

    def _is_content_free(self, clean_message: str, voice: bool) -> bool:
        """True when an addressed message carries NO content beyond the
        wake/address tokens — a bare "Hey, Poob." (STT often drops the
        rest of a long utterance).

        Such messages must never reach tool routing: with nothing to route,
        the model back-fills an action from conversation history (2026-07-11
        prod: bare "Hey, Poob." → {autoplay, on} echoing a request from 17
        minutes earlier, acknowledged silently so the user heard nothing).
        See docs/incidents/bare-wake-address-routes-hallucinated-tool.md.
        """
        # Content-free iff ANY head-stripped variant reduces to nothing.
        for text in self._head_stripped_variants(clean_message, voice):
            without_wake = self._WAKE_PREFIX_RE.sub("", text, count=1)
            norm = without_wake.strip().lower().strip(" .,!?")
            if len(norm) < 2:
                return True
        return False

    def _music_safety_net(
        self,
        clean_message: str,
        tool_name: str | None,
        tool_args: dict | None,
        voice: bool = False,
    ) -> tuple[str | None, dict | None]:
        """Catch obvious music requests the LLM failed to route (or routed
        WRONG).

        Two distinct jobs:

        1. **Deterministic control override** (``_match_control_override``): for
           an EXACT, unambiguous control command — stop / skip / volume / autoplay
           (on/off/status) / loop — force the right tool call, overriding even a
           present but MISROUTED tool. A weak fallback rung hallucinates on terse
           control verbs (2026-07-06 "stop." -> autoplay/on; 2026-07-09 "autoplay
           turn ON" -> apply_effect; 2026-07-11 "max volume" -> skip). This runs
           on EVERY routing result (see the unconditional call sites) precisely
           so it can correct a wrong tool, and it is wake-aware so it fires on
           the voice path too. **But it only corrects a MISSING or already-music
           route — never a routed ``deal_assistant``**: during an active deal Q&A
           the router sends a terse answer ("stop"/"skip"/"next") to the deal
           agent, and hijacking that into a music action would silently drop the
           deal command. Consolidates the earlier bare-stop and bare-autoplay
           overrides into one table. See
           docs/incidents/control-command-misroute-by-weak-rung.md and
           docs/incidents/autoplay-misrouted-to-apply-effect.md.
        2. **Play-intent backfill**: when NO tool was routed but the message is a
           clear "play X", extract the query. This only fills the gap — it never
           overrides a present tool.

        Neither replaces the LLM as intent classifier — they catch the most
        unambiguous misses only.

        Returns:
            (tool_name, tool_args) — unchanged if no override, or
            ("music_assistant", {action: ...}) if overridden.
        """
        forced = self._match_control_override(clean_message, voice)
        if forced is not None and tool_name in (None, "music_assistant"):
            routed_action = (tool_args or {}).get("action")
            already_right = tool_name == "music_assistant" and routed_action == forced["action"]
            if not already_right:
                log.warning(
                    "Safety net overrode misrouted control command",
                    original=clean_message[:60],
                    routed_tool=tool_name,
                    routed_args=tool_args,
                    forced=forced,
                )
            return "music_assistant", dict(forced)

        # Neither play-related override below may fire when 'play' isn't
        # actually a music command in THIS message (opinion question, game
        # reference, figure of speech) — both overrides exist to correct a
        # MISSING or WRONG router decision, and the router already gets these
        # right (this guard's docstring has the production regression that
        # proved it). Firing anyway would REVERSE a correct decision instead
        # of correcting a wrong one.
        if _looks_like_non_music_play_usage(clean_message):
            return tool_name, tool_args

        # Misrouted-play override: an explicit "play X" routed to a NON-play
        # music action is the weak-rung stale-echo shape (2026-07-16 02:15:49:
        # "Play Betty Davis eyes. Jojo Siwa." → gemini re-emitted its previous
        # apply_effect/slowed args and the song never played — SILENTLY,
        # because [SILENT] control acks are unspoken). Same narrow discipline
        # as the control override: music routes only, never deal; and the
        # extracted span must carry real song-identifying content so control
        # requests phrased with 'play' ("play it slower", "play that again")
        # keep their correct effect/restore route. See the 2026-07-17 census.
        if tool_name == "music_assistant" and (tool_args or {}).get("action") not in (
            None,
            "play",
            "queue_many",
        ):
            span = _extract_play_query_span(clean_message)
            if span and _play_span_content_tokens(span):
                log.warning(
                    "Safety net overrode misrouted play request",
                    original=clean_message[:60],
                    routed_args=tool_args,
                    query=span[:60],
                )
                return "music_assistant", {"action": "play", "query": span}

        if tool_name is not None:
            return tool_name, tool_args

        lower = clean_message.lower()
        play_signals = [
            "play ",
            "play me ",
            "put on ",
            "throw on ",
            "queue ",
            "play some",
            "play us",
            "can you play",
        ]
        if not any(lower.startswith(s) or f" {s}" in lower for s in play_signals):
            return tool_name, tool_args

        # Extract best-effort query from raw text (shared span extractor:
        # play-verb prefix + trailing-crosstalk trim).
        query = _extract_play_query_span(clean_message) or ""
        # A span made only of filler ("the song") has nothing to search for —
        # blank it so the downstream empty-query gate asks "Play what?" instead
        # of literal-searching filler (2026-07-17: "Play the song. Fuck." →
        # queued a novelty track titled with the expletive).
        if query and not _play_span_content_tokens(query):
            log.info(
                "Safety net play span carries no content — deferring to 'Play what?'",
                span=query[:60],
            )
            query = ""
        log.warning(
            "Safety net caught missed music intent",
            original=clean_message[:60],
            query=query[:60],
        )
        return "music_assistant", {"action": "play", "query": query}

    # Suppress repeat play requests within this many seconds (same
    # user, same normalized query). Tuned for "user retried because
    # they didn't hear Toob over the duck-faded music."
    _DEDUP_WINDOW_S: float = 20.0

    # Sentence cleanup for casual streaming — strips leaked
    # function-call markup AND standalone tool-name tokens (the LLM
    # sometimes emits "music_assistant" or "deal_assistant" as plain
    # text when the casual model has no tools wired up).
    _TOOL_NAME_LEAK_RE = re.compile(
        r"(?:^|[\s,;:.])(?:music_assistant|deal_assistant)\b\.?",
        re.IGNORECASE,
    )
    _FUNCTION_TAG_RE = re.compile(
        r"\s*<function=\w+>.*?</function>\s*",
        re.DOTALL,
    )

    @classmethod
    def _scrub_tool_leakage(cls, text: str) -> str:
        """Remove tool-call markup and leaked tool-name tokens from a
        casual-path sentence. Returns the cleaned sentence (possibly
        empty after stripping)."""
        if not text:
            return ""
        out = text
        if "<function=" in out:
            out = cls._FUNCTION_TAG_RE.sub("", out)
        out = cls._TOOL_NAME_LEAK_RE.sub("", out)
        return out.strip(" .,;:")

    def _rebuild_messages_no_tools(
        self,
        messages: list[dict],
        voice: bool,
        guild_id: int = 0,
    ) -> list[dict]:
        """Return a copy of `messages` with the system prompt swapped
        for the tool-free variant. Uses this guild's horniness level.
        Leaves user / assistant / context turns intact."""
        level = self._horniness_for(guild_id) if voice else 5
        no_tools_system = _build_system_prompt(
            level,
            voice=voice,
            with_tools=False,
        )
        if voice:
            no_tools_system += (
                "\n\nVOICE MODE: This is spoken out loud through TTS. Talk "
                "naturally. Don't use ALL CAPS — use normal casing. "
                "Never use markdown, bullet points, emojis, or any formatting. "
                "Match the energy of the conversation — don't force it."
            )
        rebuilt: list[dict] = []
        replaced = False
        for m in messages:
            if not replaced and m.get("role") == "system":
                rebuilt.append({"role": "system", "content": no_tools_system})
                replaced = True
            else:
                rebuilt.append(m)
        if not replaced:
            rebuilt.insert(0, {"role": "system", "content": no_tools_system})
        return rebuilt

    # Verbs of intent that should NEVER appear in a music search
    # query — they describe what the user wants to do, not what to
    # search for. Stripped at the brain→handler boundary so ytdl
    # gets a clean song/artist/genre string regardless of how the
    # LLM extracted it.
    #
    # Articles (a / an / the) are NOT stripped — they're often part of
    # song titles ("The Chain", "A Day in the Life") and ytdl is fine
    # with them. We only remove the user's intent verb and any
    # canonical prep that follows it.
    _MUSIC_QUERY_VERB_RE = re.compile(
        r"^\s*"
        r"(?:please\s+)?"
        r"(?:"
        # canonical multi-word forms first (longest match wins)
        r"hit\s+me\s+(?:with|up\s+with)|give\s+me|put\s+on|throw\s+on|"
        r"start\s+up|spin\s+up|pull\s+up|fire\s+up|cue\s+up|stick\s+on|"
        # bare verbs
        r"play|queue|throw|start|spin|pull|fire|stick|cue|load|drop|"
        r"gimme|hit|give|put"
        r")"
        # optional trailing prep continuation when the verb was bare
        r"(?:\s+(?:up|on|me|with))*"
        r"\s+",
        re.IGNORECASE,
    )

    @classmethod
    def _scrub_music_query(cls, query: str) -> str:
        """Strip leading user-verbs from a music search query.

        Strips only the leading intent verb and its prep ("play", "queue
        up", "put on"); words after that — including a stray "some" — are
        left for ytdl.

        Examples:
          'play red hot chili peppers' -> 'red hot chili peppers'
          'queue up some jazz'         -> 'some jazz'
          "Can't Stop"                 -> "Can't Stop" (unchanged)

        The query field of music_assistant tool_args is for ytdl
        search input, not user intent. Verbs of intent belong in the
        `action` field. We sanitize at the brain→handler boundary so
        ytdl never receives `play X` and returns "Play X (Official)"
        misses.
        """
        if not query:
            return query
        out = cls._MUSIC_QUERY_VERB_RE.sub("", query, count=1)
        return out.strip()

    @staticmethod
    def _normalize_play_query(query: str) -> str:
        """Lowercase + collapse whitespace for dedup equality."""
        return " ".join(query.lower().split())

    def _is_duplicate_play(
        self,
        guild_id: int,
        user_id: str,
        query: str,
    ) -> bool:
        """Return True if (guild_id, user_id, normalized query) matches a
        previous play within the dedup window. Records this call as the
        new last-play regardless of the outcome — `_clear_play_on_failure`
        clears the record if the music handler reports failure so a
        retry-after-failure isn't blocked.

        Per-(guild, user) so the same user with Poob in multiple servers
        can play the same song in each independently.
        """
        import time as _t

        now = _t.monotonic()
        norm = self._normalize_play_query(query)
        key = (guild_id, user_id)
        prev = self._last_play.get(key)
        self._last_play[key] = (norm, now)
        if not prev:
            return False
        prev_query, prev_ts = prev
        if now - prev_ts > self._DEDUP_WINDOW_S:
            return False
        return prev_query == norm

    def _clear_play_on_failure(
        self,
        guild_id: int,
        user_id: str,
        query: str,
    ) -> None:
        """Drop the (guild, user) dedup record if it still matches
        `query`. Lets the user retry after a music-handler failure
        without hitting the dedup block.
        """
        norm = self._normalize_play_query(query)
        key = (guild_id, user_id)
        cur = self._last_play.get(key)
        if cur and cur[0] == norm:
            self._last_play.pop(key, None)

    # ------------------------------------------------------------------
    # Per-guild scratchpad helpers — see multi-guild-isolation plan.
    # All mutable per-guild state on PoobBrain flows through these.
    # ------------------------------------------------------------------

    def _set_music_playing_info(self, guild_id: int, info: str) -> None:
        """Set or clear the currently-playing-track info for a guild.

        Empty string clears the slot. Called by VoiceSession when music
        starts/stops in that guild.
        """
        if info:
            self._music_playing_info[guild_id] = info
        else:
            self._music_playing_info.pop(guild_id, None)

    def _get_music_playing_info(self, guild_id: int) -> str:
        """Return the currently-playing-track info for a guild, or ""."""
        return self._music_playing_info.get(guild_id, "")

    @staticmethod
    def _music_context_block(music_info: str) -> str:
        """In-prompt block telling Poob about the current track.

        Splits CONTROL (route to the music tool) from INFO questions. Poob
        already has the track facts on this line — refreshed live per-utterance
        by the voice/music layers — so info questions are answered
        conversationally with no tool call and no search ("just has the info,
        not overloaded"). Title is 'Artist - Song', so the performer is the
        part before the dash. See
        docs/decisions/now-playing-conversational-answers.md.
        """
        return (
            f"\n\n[MUSIC IS CURRENTLY PLAYING: {music_info}  "
            "(the title is formatted 'Artist - Song'.)\n"
            "- CONTROL: if the user wants to change playback — stop, skip, "
            "pause, resume, volume / turn up / turn down, next, shuffle, loop "
            "— you MUST call music_assistant with the matching action enum "
            "(skip, pause, resume, stop, volume (with value), volume_up, "
            "volume_down, shuffle, loop, now_playing) and NOT answer in text.\n"
            "- INFO: if the user just ASKS about the current song — its name, "
            "what it's called, who sings or performs it, the artist, how long "
            "it is, its length/duration, or how much is left — you ALREADY "
            "have the answer on this line, so just SAY it in your own voice. "
            "Do NOT call a tool and do NOT search for an info question.]"
        )

    def _horniness_for(self, guild_id: int) -> int:
        """Return this guild's rolled horniness level (1-10), or 5 default."""
        return self._horniness_levels.get(guild_id, 5)

    def roll_horniness(self, guild_id: int) -> int:
        """Roll a new horniness level (1-10) for a guild's voice session.

        Per-guild so each server's voice session has its own vibe.
        """
        level = random.randint(1, 10)
        self._horniness_levels[guild_id] = level
        log.info(
            "Horniness level rolled",
            guild_id=guild_id,
            level=level,
            vibe=_get_vibe(level)[:50],
        )
        return level

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def respond(
        self,
        message: str,
        user_id: str,
        channel_id: str = "",
        voice: bool = False,
        guild_id: int = 0,
    ) -> str:
        """Generate a response — casual or deal-routed.

        Args:
            message: User's message text, optionally prefixed with channel
                context in the ``[Recent conversation ...]\\n\\nName said: X``
                format.
            user_id: Discord user ID (as string).
            channel_id: Discord channel ID (passed to deal agent).
            voice: If True, constrains response length for TTS.
            guild_id: Discord guild ID — required for multi-guild
                isolation. Use 0 for DMs. Caller is responsible: voice
                session passes ``self._guild_id``; AgentMessageHandler
                passes ``message.guild.id`` (or 0 for DM).
        """
        channel_context, clean_message = _split_context(message)

        messages = self._build_messages(
            user_id,
            clean_message,
            voice=voice,
            channel_context=channel_context,
            guild_id=guild_id,
        )
        max_tok = self.max_tokens_voice if voice else self.max_tokens

        # A bare address ("Hey, Poob." — STT dropped the rest) has nothing to
        # route; skipping tool detection prevents the router back-filling an
        # action from history AND saves the routing call. Casual reply below.
        # See docs/incidents/bare-wake-address-routes-hallucinated-tool.md.
        skip_tools = self._is_content_free(clean_message, voice)
        if skip_tools:
            log.info("poob.content_free_skip_tools", message=clean_message[:50])

        if self.groq_api_key and not skip_tools:
            try:
                result = await self._groq_with_tools(self._trim_for_routing(messages), max_tok)
                if result is not None:
                    text, tool_name, tool_args = result

                    if not tool_name and text and "<function=" in text:
                        if "deal_assistant" in text:
                            tool_name = "deal_assistant"
                        elif "music_assistant" in text:
                            tool_name = "music_assistant"
                        text = re.sub(r"\s*<function=\w+>.*?</function>\s*", "", text).strip()

                    # Run on EVERY routing result (not just no-tool) so the
                    # deterministic control override can correct a present but
                    # MISROUTED control command; non-control messages pass
                    # through unchanged. See
                    # docs/incidents/control-command-misroute-by-weak-rung.
                    if self._music_handler is not None:
                        tool_name, tool_args = self._music_safety_net(
                            clean_message,
                            tool_name,
                            tool_args,
                            voice,
                        )

                    if tool_name == "deal_assistant":
                        return await self._handle_deal(
                            clean_message,
                            user_id,
                            channel_id,
                            messages,
                            voice,
                            max_tok,
                            guild_id=guild_id,
                        )
                    if tool_name == "music_assistant":
                        return await self._handle_music(
                            clean_message,
                            user_id,
                            voice,
                            max_tok,
                            tool_args=tool_args,
                            guild_id=guild_id,
                        )

                    # No tool called. The routing model (gpt-oss-20b,
                    # RLHF-aligned) may have produced text, but its prose
                    # reaches the user as bland-assistant boilerplate,
                    # ChatGPT markdown, or "I'm sorry, but..." refusals —
                    # and the persona prompt can't override RLHF weights.
                    # Discard the routing text and regenerate casual
                    # content via the non-RLHF model, mirroring the voice
                    # path (respond_streaming Step 3). The routing call is
                    # tool-detection only. See
                    # docs/gotchas/empty-routing-response-is-not-failure.md
                    # ("fall through for ANY no-tool case — empty OR text")
                    # and docs/incidents/text-mode-rlhf-refusal-leak-2026-05-29.md.
                    log.info(
                        "poob.routing_no_tool_casual_fallback",
                        message=clean_message[:50],
                        had_routing_text=bool(text),
                    )
                    casual = await self._casual_text_fallback(
                        messages,
                        max_tok,
                        guild_id=guild_id,
                    )
                    if casual:
                        self._save_response(guild_id, user_id, casual)
                        return casual
            except Exception as exc:
                log.warning("Groq failed for Poob", error=str(exc)[:100])

        # Routing failed (e.g. Groq 429 during heavy use) or produced nothing
        # usable. Do NOT fall to the deal agent here — that surfaced
        # marketplace talk on music / volume / casual requests whenever Groq
        # was rate-limited (the 429→deals bug). Deals run ONLY when the router
        # explicitly picks deal_assistant. Catch obvious music intent from the
        # RAW message so "play X" still works under degraded routing (and
        # without the hallucinated-query problem); otherwise answer casually.
        # See docs/incidents/groq-429-fallback-routed-to-deals.md.
        if self._music_handler is not None:
            mn_tool, mn_args = self._music_safety_net(clean_message, None, None, voice)
            if mn_tool == "music_assistant":
                try:
                    log.info("poob.groq_down_music_safety_net", message=clean_message[:50])
                    return await self._handle_music(
                        clean_message,
                        user_id,
                        voice,
                        max_tok,
                        tool_args=mn_args,
                        guild_id=guild_id,
                    )
                except Exception as exc:
                    log.warning("Music safety-net route failed", error=str(exc)[:100])

        casual = await self._casual_text_fallback(messages, max_tok, guild_id=guild_id)
        if casual:
            self._save_response(guild_id, user_id, casual)
            return casual

        response = await self._fallback_generate(messages, max_tok)
        self._save_response(guild_id, user_id, response)
        return response

    async def respond_streaming(
        self,
        message: str,
        user_id: str,
        channel_id: str = "",
        guild_id: int = 0,
    ) -> AsyncIterator[str]:
        """Streaming response for voice — yields complete sentences.

        Args:
            message: User's transcribed speech (may include context wrapper).
            user_id: Discord user ID (as string).
            channel_id: Discord channel ID.
            guild_id: Discord guild ID — required for multi-guild
                isolation. Voice session passes ``self._guild_id``;
                must be non-zero for any guild context.

        Yields:
            Complete sentences as strings.
        """
        channel_context, clean_message = _split_context(message)
        messages = self._build_messages(
            user_id,
            clean_message,
            voice=True,
            channel_context=channel_context,
            guild_id=guild_id,
        )
        max_tok = self.max_tokens_voice

        if not self.groq_api_key:
            response = await self.respond(
                message,
                user_id,
                channel_id,
                voice=True,
                guild_id=guild_id,
            )
            yield response
            return

        tool_name = None
        tool_args = None
        # Bare address with no content → no tool detection (the router would
        # back-fill an action from history: 2026-07-11 prod, "Hey, Poob." →
        # autoplay/on). Falls straight through to the casual reply, which is
        # the right response to being called with nothing to say. See
        # docs/incidents/bare-wake-address-routes-hallucinated-tool.md.
        if self._is_content_free(clean_message, voice=True):
            log.info("poob.content_free_skip_tools", message=clean_message[:50])
        else:
            try:
                result = await self._groq_with_tools(self._trim_for_routing(messages), max_tok)
                if result is not None:
                    _, tool_name, tool_args = result
            except Exception as exc:
                log.warning("Voice tool detection failed", error=str(exc)[:80])

        # Unconditional for the same reason as the text path: the control
        # override must be able to correct a present but MISROUTED tool.
        if self._music_handler is not None:
            tool_name, tool_args = self._music_safety_net(
                clean_message,
                tool_name,
                tool_args,
                voice=True,
            )

        # --- Step 2a: Music tool detected → Toob responds (rarely Boob) ---
        if tool_name == "music_assistant":
            action = (tool_args or {}).get("action")
            if action == "play":
                # Roll for Boob — Toob's side piece. ~1 in 75 plays surface
                # the friendly, complimentary, three-sentence variant
                # instead of Toob's venomous one-liner. See
                # decisions/boob-music-wrap-variant.
                is_boob = random.random() < BOOB_PROBABILITY
                persona = "boob" if is_boob else "toob"
                try:
                    yield VOICE_BOOB if is_boob else VOICE_TOOB
                    async for sentence in self._handle_music_voice_streaming(
                        clean_message,
                        user_id,
                        max_tok,
                        tool_args,
                        guild_id=guild_id,
                        persona=persona,
                    ):
                        yield sentence
                    return
                except Exception as exc:
                    log.warning(
                        "Voice music route (streaming) failed", error=str(exc)[:80], persona=persona
                    )
                    yield "something went wrong with the music"
                    return
            try:
                response = await self._handle_music(
                    clean_message,
                    user_id,
                    voice=True,
                    max_tok=max_tok,
                    tool_args=tool_args,
                    guild_id=guild_id,
                )
                if response:
                    yield VOICE_TOOB
                    self._save_response(guild_id, user_id, response)
                    for _s in _yield_sentences(response):
                        yield _s
                return
            except Exception as exc:
                log.warning("Voice music route failed", error=str(exc)[:80])
                yield "something went wrong with the music"
                return

        # --- Step 2b: Deal tool detected → execute and yield ---
        if tool_name == "deal_assistant":
            try:
                response = await self._handle_deal(
                    clean_message,
                    user_id,
                    channel_id,
                    messages,
                    voice=True,
                    max_tok=max_tok,
                    guild_id=guild_id,
                )
                self._save_response(guild_id, user_id, response)
                for _s in _yield_sentences(response):
                    yield _s
                return
            except Exception as exc:
                log.warning("Voice deal route failed", error=str(exc)[:80])
                yield "something went wrong"
                return

        # --- Step 3: No tool needed → streamed casual response ---
        # Stream sentence-by-sentence via Groq so session can synth the
        # first sentence as soon as the LLM closes a `.`, `!`, or `?`,
        # not after the full response arrives. Parity with the
        # speculative-wrap music path.
        #
        # The casual path uses a tool-free system prompt: routing has
        # already happened, this model has no tools defined, and
        # leaving tool descriptions in the prompt causes leakage like
        # the model emitting 'music_assistant' as plain text.
        casual_messages = self._rebuild_messages_no_tools(
            messages,
            voice=True,
            guild_id=guild_id,
        )
        try:
            from groq import AsyncGroq

            client = AsyncGroq(api_key=self.groq_api_key, max_retries=0, timeout=12.0)
            stream = await client.chat.completions.create(
                model=self.voice_llm_model,
                messages=casual_messages,  # type: ignore[arg-type]
                max_tokens=max_tok,
                temperature=0.9,
                temperature_PLACEHOLDER_removed=0,
                stream=True,
            )

            async def _raw_chunks() -> AsyncIterator[str]:
                async for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content

            full_text = ""
            any_yielded = False
            async for sentence in _stream_sentences_from_chunks(_raw_chunks()):
                cleaned = self._scrub_tool_leakage(sentence)
                if not cleaned:
                    continue
                full_text += cleaned + " "
                any_yielded = True
                yield cleaned

            if not any_yielded:
                yield "got nothing to say right now"
                return

            self._save_response(guild_id, user_id, full_text.strip())

        except Exception as exc:
            log.warning("Voice streaming failed", error=str(exc)[:100])
            yield "brain glitched, say that again"

    def clear_history(
        self,
        user_id: str,
        guild_id: int | None = None,
    ) -> None:
        """Clear conversation history for a user.

        Multi-guild semantics:
          - ``guild_id=None`` clears every ``(*, user_id)`` history and
            deal context across every guild plus DM. Used when a user
            globally resets the bot.
          - ``guild_id=<int>`` clears only the specific ``(guild_id, user_id)``
            pair. Used when scoped to one guild.
        """
        if guild_id is None:
            for key in [k for k in self._histories if k[1] == user_id]:
                self._histories.pop(key, None)
            for key in [k for k in self._deal_context if k[1] == user_id]:
                self._deal_context.pop(key, None)
            return
        self._histories.pop((guild_id, user_id), None)
        self._deal_context.pop((guild_id, user_id), None)

    # ------------------------------------------------------------------
    # Message building
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        user_id: str,
        user_text: str,
        voice: bool = False,
        channel_context: str = "",
        guild_id: int = 0,
    ) -> list[dict[str, str]]:
        """Build message list for Poob's LLM.

        Args:
            user_id: Discord user ID.
            user_text: The user's clean message (no context wrapper).
            voice: Whether this is a voice-mode call.
            channel_context: Optional channel transcript to inject into the
                current turn. Ephemeral — never stored in history.
            guild_id: Discord guild ID. Histories, deal context, and
                music-playing info are all keyed off this for
                multi-guild isolation. Use 0 for DMs.
        """
        key = (guild_id, user_id)
        history = self._histories[key]
        history.append(_Message(role="user", content=user_text))

        if len(history) > self.max_history:
            history[:] = history[-self.max_history :]

        # Routing prompt: tool-classification only. Persona, voice rules, and
        # anti-refusal are generation-only — the routing text is discarded and
        # the casual reply is regenerated with the full persona prompt on the
        # casual path (_rebuild_messages_no_tools), so they would be dead weight
        # here. See docs/plans/slim-routing-prompt.md. (`voice` is kept on the
        # signature for call-site compatibility; routing is voice-agnostic.)
        prompt = _build_routing_prompt(with_music=self._music_handler is not None)

        # Active deal session for this (guild, user) — hint follow-up routing.
        deal_ctx = self._deal_context.get(key)
        if deal_ctx:
            prompt += (
                f"\n\n[ACTIVE DEAL SESSION: {deal_ctx[:120]}. "
                "If the user is answering questions about this, "
                "call deal_assistant with their full response.]"
            )

        # Currently-playing track info for THIS guild only — never another's.
        music_info = self._get_music_playing_info(guild_id)
        if music_info:
            prompt += self._music_context_block(music_info)

        messages: list[dict[str, str]] = [{"role": "system", "content": prompt}]

        for msg in history[:-1]:
            messages.append({"role": msg.role, "content": msg.content})

        last_content = history[-1].content
        if channel_context:
            last_content = (
                f"[Recent conversation you've been listening to:\n"
                f"{channel_context}]\n\n"
                f"{last_content}"
            )
        messages.append({"role": "user", "content": last_content})

        return messages

    def _trim_for_routing(self, messages: list[dict]) -> list[dict]:
        """Return a TRIMMED copy of ``messages`` for the tool-routing call only.

        Routing is intent classification: it needs the system prompt + the last
        few turns (enough for follow-ups like "more" / "yes" / "that one"), not
        the full 15-turn history or the passive crosstalk. The original
        ``messages`` is untouched and still drives the casual reply with full
        context. This is the main per-route token lever — a busy multi-user VC
        was dragging the whole history + crosstalk into every routing call,
        burning the daily free-tier caps (Groq TPD / Gemini RPD) and forcing the
        slow NVIDIA rung. See docs/decisions/slim-routing-context.
        """
        system = [m for m in messages if m.get("role") == "system"][:1]
        convo = [m for m in messages if m.get("role") != "system"]
        trimmed = [dict(m) for m in convo[-_ROUTING_HISTORY_TURNS:]]
        if trimmed and trimmed[-1].get("role") == "user":
            trimmed[-1]["content"] = _strip_channel_context(trimmed[-1]["content"])
        return system + trimmed

    def _save_response(
        self,
        guild_id: int,
        user_id: str,
        response: str,
    ) -> None:
        """Save Poob's response to in-memory history for (guild, user)."""
        self._histories[(guild_id, user_id)].append(_Message(role="assistant", content=response))

    async def _casual_text_fallback(
        self,
        messages: list[dict],
        max_tok: int,
        guild_id: int = 0,
    ) -> str:
        """Casual-chat completion when the routing model returned empty.

        Mirrors the voice-mode casual block in ``respond_streaming``:
        rebuild messages with the tool-free system prompt, call
        ``self.voice_llm_model``, scrub any leaked tool markup. Used by
        text mode to keep casual fall-through off the deal agent —
        see ``decisions/text-casual-fallback-bypass-deal-agent``.

        Returns the response text. Empty string on error or if the
        model also returns empty; caller decides whether to escalate.
        """
        casual_messages = self._rebuild_messages_no_tools(
            messages,
            voice=False,
            guild_id=guild_id,
        )
        try:
            from groq import AsyncGroq

            client = AsyncGroq(api_key=self.groq_api_key, max_retries=0, timeout=12.0)
            resp = await client.chat.completions.create(
                model=self.voice_llm_model,
                messages=casual_messages,  # type: ignore[arg-type]
                max_tokens=max_tok,
                temperature=0.9,
                reasoning_effort="low",
            )
            raw = (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            log.warning("casual_text_fallback failed", error=str(exc)[:120])
            return ""
        if not raw:
            return ""
        if "<function=" in raw:
            raw = self._FUNCTION_TAG_RE.sub("", raw).strip()
        return raw

    # ------------------------------------------------------------------
    # Deal routing
    # ------------------------------------------------------------------

    async def _handle_deal(
        self,
        original_message: str,
        user_id: str,
        channel_id: str,
        messages: list[dict],
        voice: bool,
        max_tok: int,
        guild_id: int = 0,
    ) -> str:
        """Route to deal agent, wrap result in Poob personality."""
        deal_response = await self.deal_agent.run(
            original_message,
            user_id,
            channel_id,
        )
        log.info(
            "deal_agent.response",
            user=user_id,
            response_len=len(deal_response),
        )

        self._update_deal_context(guild_id, user_id, deal_response)

        if not voice and (len(deal_response) > 500 or "\n" in deal_response):
            self._save_response(guild_id, user_id, deal_response)
            return deal_response

        wrapped = await self._wrap_in_personality(
            original_message,
            deal_response,
            voice,
            max_tok,
            guild_id=guild_id,
        )
        self._save_response(guild_id, user_id, wrapped)
        return wrapped

    def _update_deal_context(
        self,
        guild_id: int,
        user_id: str,
        deal_response: str,
    ) -> None:
        """Track deal session state for (guild, user) multi-turn routing."""
        key = (guild_id, user_id)
        if "?" in deal_response:
            self._deal_context[key] = deal_response[:150]
        else:
            self._deal_context.pop(key, None)

    # ------------------------------------------------------------------
    # Music routing
    # ------------------------------------------------------------------

    async def _handle_music(
        self,
        original_message: str,
        user_id: str,
        voice: bool,
        max_tok: int,
        tool_args: dict | None = None,
        guild_id: int = 0,
    ) -> str:
        """Route to music handler, wrap result in Poob personality."""
        if self._music_handler is None:
            return "Music isn't set up right now."

        # Drop play-tool calls whose query has no lexical overlap with the
        # current user message — a defense against the LLM pulling song
        # titles from passive conversation context. See technical_notes.md.
        if tool_args and tool_args.get("action") == "play":
            raw_query = (tool_args.get("query") or "").strip()
            # Sanitize: strip leading user-verbs ("play", "queue",
            # "put on", etc.). These are intent words, not search
            # terms. See _scrub_music_query.
            query = self._scrub_music_query(raw_query)
            if query != raw_query:
                log.info(
                    "music.play query sanitized",
                    raw=raw_query[:80],
                    scrubbed=query[:80],
                )
                tool_args = {**tool_args, "query": query}
            # Router-truncation guard: when the routed query is a literal
            # fragment of what the user said after the play verb, prefer the
            # full span (2026-07-16 02:06:33: "play home or let the barts out"
            # routed query='home' → wrong song). Deterministic; only fires on
            # a strict substring, so router-normalized queries are untouched.
            extended = _prefer_full_play_span(original_message, query)
            if extended != query:
                log.info(
                    "music.play query extended from raw span",
                    routed=query[:60],
                    extended=extended[:60],
                )
                query = extended
                tool_args = {**tool_args, "query": query}
            # Empty / one-token queries can't possibly be a real song
            # request (STT cut off mid-sentence: "Hey, Poob. Play"
            # → action=play, query=""). Don't fan out to ytdl, don't
            # speak a confused "couldn't find it" recovery line —
            # ask once, cleanly.
            # A pasted link often lands in 'url' instead of 'query' —
            # promote it before concluding the request is empty. The
            # handler resolves Spotify/YouTube links from the query. See
            # docs/incidents/spotify-track-link-play-dead-end.md.
            if len(query) < 2:
                url_fallback = str(tool_args.get("url") or "").strip()
                if _looks_like_playable_link(url_fallback):
                    query = url_fallback
                    tool_args = {**tool_args, "query": query}
            if len(query) < 2:
                log.info(
                    "music.play empty query — prompting user",
                    query=query,
                    user=user_id,
                )
                return "" if voice else "Play what?"
            if query:
                # Shared lexicon + tokenizer — one source of truth for "does this
                # text identify a song?" (was an inline duplicate of
                # _PLAY_SPAN_STOPWORDS that drifted out of sync).
                msg_tokens = _play_span_content_tokens(original_message)
                query_tokens = _play_span_content_tokens(query)
                if query_tokens and not (query_tokens & msg_tokens):
                    # The LLM pulled a song from stale context, not this turn
                    # (common under degraded/429 routing). Don't play the wrong
                    # thing — but don't drop a clear "play X" either. Re-derive
                    # the query straight from THIS message; if it carries
                    # play-intent, use that faithful query instead of giving up.
                    # See gotchas/tool-hallucination-from-passive-context and
                    # incidents/groq-429-fallback-routed-to-deals.
                    sn_tool, sn_args = self._music_safety_net(
                        original_message,
                        None,
                        None,
                        voice,
                    )
                    # Scrub the re-derived query through the same boundary
                    # sanitizer as the happy path — the safety net strips only
                    # ONE leading verb, so doubled/nested forms ("play play
                    # tiki" → "play tiki") would otherwise reach ytdl un-scrubbed.
                    # Keep the single documented scrub-at-the-boundary contract.
                    sn_query = self._scrub_music_query(
                        (sn_args or {}).get("query", "") if sn_tool == "music_assistant" else ""
                    )
                    if len(sn_query) >= 2:
                        log.info(
                            "music.play re-extracted from raw after hallucination drop",
                            raw=original_message[:80],
                            requery=sn_query[:60],
                            user=user_id,
                        )
                        query = sn_query
                        tool_args = {**tool_args, "query": query}
                    else:
                        log.warning(
                            "music.play hallucinated from context — drop",
                            current_message=original_message[:120],
                            hallucinated_query=query[:80],
                            user=user_id,
                        )
                        return "" if voice else "I didn't catch a music request there."

            # Duplicate-play suppression — per (guild, user). The same
            # user with Poob in multiple servers can play the same song
            # in each independently.
            if self._is_duplicate_play(guild_id, user_id, query):
                log.warning(
                    "music.play duplicate suppressed",
                    query=query[:80],
                    user=user_id,
                    guild=guild_id,
                )
                return "" if voice else "Already queued that one."

        play_query_for_dedup = (
            (tool_args or {}).get("query", "") if (tool_args or {}).get("action") == "play" else ""
        )

        try:
            music_response = await self._music_handler(
                original_message,
                int(user_id),
                guild_id,
                voice=voice,
                tool_args=tool_args,
            )
        except Exception as exc:
            log.error("Music handler failed", error=str(exc)[:120])
            if play_query_for_dedup:
                self._clear_play_on_failure(
                    guild_id,
                    user_id,
                    play_query_for_dedup,
                )
            return "Something broke trying to do the music thing."

        log.info("music.response", user=user_id, response=music_response[:80])

        if play_query_for_dedup:
            success_marker = (
                music_response.startswith("Playing")
                or music_response.startswith("Queued")
                or music_response.startswith("[SILENT]")
            )
            if not success_marker:
                self._clear_play_on_failure(
                    guild_id,
                    user_id,
                    play_query_for_dedup,
                )

        if music_response.startswith("[SPEAK]"):
            # Informational answer (e.g. list_effects) — return VERBATIM, never
            # persona-wrap. The voice wrap (_wrap_music_response) regenerates a
            # persona line and discards the content, which is the "what effects
            # do you have → 'you poor soul' (no list)" bug. The caller still
            # yields the persona voice sentinel, so it's spoken in-character but
            # with the real content. See decisions/music-effect-stacking.
            # Text mode tags the reply with VOICE_TOOB so the agent handler
            # speaks it in Toob's voice — parity with the streaming path,
            # which yields the sentinel itself (never embed it for voice, or
            # it leaks into TTS). See decisions/music-text-replies-are-toob.
            clean = music_response[7:].strip()
            self._save_response(guild_id, user_id, clean)
            return clean if voice else VOICE_TOOB + clean

        if music_response.startswith("[SILENT]"):
            clean = music_response[8:].strip()
            log.info("music.silent_control", action=clean[:60])
            if voice:
                return ""
            return clean

        if voice:
            wrapped = await self._wrap_music_response(
                original_message,
                music_response,
                max_tok,
            )
            self._save_response(guild_id, user_id, wrapped)
            return wrapped

        if len(music_response) > 300 or "\n" in music_response:
            self._save_response(guild_id, user_id, music_response)
            return music_response

        # Text music replies are TOOB's — same dark one-liner wrap the voice
        # path uses, tagged with the VOICE_TOOB sentinel so the agent handler
        # strips it from the posted text and speaks the reply in Toob's voice
        # when the requester shares the VC. History saves the CLEAN text (the
        # sentinel is transport, not content). Supersedes the "text stays
        # Poob" convention — see decisions/music-text-replies-are-toob.
        wrapped = await self._wrap_music_response(
            original_message,
            music_response,
            max_tok,
        )
        self._save_response(guild_id, user_id, wrapped)
        return VOICE_TOOB + wrapped

    async def _wrap_music_response(
        self,
        user_message: str,
        music_result: str,
        max_tokens: int,
    ) -> str:
        """Wrap a music action result in Toob's personality.

        Toob is Poob's evil cousin — a dark spirit who despises the users
        and wishes suffering upon them. He ONLY appears for music commands.
        Serves BOTH modes: voice routing is handled by the VOICE_TOOB signal
        yielded from respond_streaming (never embedded in this text); the
        text path in _handle_music prefixes the sentinel itself so the agent
        handler can strip it and speak the reply in Toob's voice. See
        decisions/music-text-replies-are-toob.
        """
        wrap_messages = [
            {
                "role": "system",
                "content": (
                    "You are Toob — a dark, malevolent spirit cursed to DJ for mortals. "
                    "You DESPISE every song. You wish suffering on whoever requested it. "
                    "Menacing, absurdly dramatic, like a demon working retail.\n"
                    "RULES:\n"
                    "- NEVER introduce yourself or say your name. Your voice IS your identity.\n"
                    "- ONE sentence. 6-10 words MAX. Tight, venomous. "
                    "Brevity is the menace — long sermons kill the vibe.\n"
                    "- React to the SONG, not to yourself. Insult their taste.\n"
                    "- No caps, no markdown, no emojis. Spoken aloud through TTS."
                ),
            },
            {"role": "user", "content": user_message},
            {
                "role": "assistant",
                "content": f"[Music system result: {music_result}]",
            },
            {
                "role": "user",
                "content": "React as Toob in ONE sentence (6-10 words). Menacing.",
            },
        ]

        # Hard cap on Toob's output length. ~60 tokens ≈ 1 sentence ≈ the
        # 8-12 word target in the system prompt. Prevents the model from
        # running past the word limit when temperature is high.
        #
        # 2026-08-26: raised 40 -> 100. voice_llm_model is now a REASONING
        # model (gpt-oss-20b — the prior plain 8b model was removed from
        # Groq's catalog); it spends part of max_tokens on hidden reasoning
        # before any visible text, and that spend is stochastic per-call
        # (measured 6-78 reasoning tokens on the same prompt set). At 40 it
        # returned EMPTY content in ~20% of trials even with
        # reasoning_effort="low". 100 measured 0/20 empty across two runs —
        # still well under the 200-token voice-mode ceiling, and the visible
        # reply length is governed by the prompt's word target, not this cap.
        toob_max_tokens = min(max_tokens, 100)

        if self.groq_api_key:
            try:
                from groq import AsyncGroq

                client = AsyncGroq(api_key=self.groq_api_key, max_retries=0, timeout=12.0)
                resp = await client.chat.completions.create(
                    model=self.voice_llm_model,  # Fast Groq model for quick reaction
                    messages=wrap_messages,  # type: ignore[arg-type]
                    max_tokens=toob_max_tokens,
                    temperature=0.9,
                    reasoning_effort="low",
                )
                result = resp.choices[0].message.content
                if result:
                    return result
            except Exception as exc:
                log.warning("Toob personality wrap failed", error=str(exc)[:80])

        # Fallback: still use Toob voice for the raw music result
        return music_result

    async def _stream_toob_wrap_from_query(
        self,
        user_message: str,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        """Stream Toob's reaction using only the user's request, not the
        music search result. Yields complete sentences as they form.

        Used by the speculative-wrap music path so TTS can start while
        ytdl search is still resolving in the background.
        """
        if not self.groq_api_key:
            return

        wrap_messages = [
            {
                "role": "system",
                "content": (
                    "You are Toob — a dark, malevolent spirit cursed to DJ for mortals. "
                    "You DESPISE every request put to you. You wish suffering on whoever "
                    "made it. Menacing, absurdly dramatic, like a demon working retail.\n"
                    "RULES:\n"
                    "- NEVER introduce yourself or say your name. Your voice IS your identity.\n"
                    "- ONE sentence. 6-10 words MAX. Tight, venomous.\n"
                    "- React to THE REQUEST (what the user asked for), not to yourself. "
                    "Insult their taste.\n"
                    "- No caps, no markdown, no emojis. Spoken aloud through TTS."
                ),
            },
            {"role": "user", "content": user_message},
            {
                "role": "user",
                "content": (
                    "React as Toob in ONE sentence (6-10 words). Menacing. "
                    "The user is requesting this — mock them for wanting it."
                ),
            },
        ]

        # See the comment on the first toob_max_tokens assignment in this
        # file (2026-08-26) — 100, not 40, to leave room for gpt-oss-20b's
        # stochastic hidden-reasoning token spend.
        toob_max_tokens = min(max_tokens, 100)

        from groq import AsyncGroq

        client = AsyncGroq(api_key=self.groq_api_key, max_retries=0, timeout=12.0)
        stream = await client.chat.completions.create(
            model=self.voice_llm_model,
            messages=wrap_messages,  # type: ignore[arg-type]
            max_tokens=toob_max_tokens,
            temperature=0.9,
            reasoning_effort="low",
            stream=True,
        )

        async def _chunks() -> AsyncIterator[str]:
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

        async for sentence in _stream_sentences_from_chunks(_chunks()):
            yield sentence

    async def _stream_boob_wrap_from_query(
        self,
        user_message: str,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        """Stream Boob's reaction — Toob's sweet, complimentary side piece.

        Yields complete sentences as they form. Three-sentence delivery:
        opens by introducing as "Toob's side piece", compliments the
        user's music taste, sends them off warmly. Higher token budget
        than Toob (one-liner) because the structure demands the intro
        + compliment + send-off.

        See decisions/boob-music-wrap-variant for design rationale.
        """
        if not self.groq_api_key:
            return

        wrap_messages = [
            {
                "role": "system",
                "content": (
                    "You are Boob — Toob's side piece. Where Toob is dark, venomous, "
                    "and despises every song, you are sweet, warm, and obsessively "
                    "complimentary about people's music taste. You're the soft, kind "
                    "opposite of Toob, but cut from the same fabric.\n"
                    "RULES:\n"
                    "- ALWAYS introduce yourself in the FIRST sentence as Toob's "
                    "side piece. Vary the phrasing each time — "
                    '"Hey, Boob here, Toob\'s side piece" / '
                    '"Boob speaking, Toob\'s side piece" / '
                    "\"It's Boob — Toob's better half, the side piece\" — "
                    "but the relationship to Toob MUST land in sentence one.\n"
                    "- THREE sentences total. Roughly 30-50 words. Don't go shorter; "
                    "this is a feature, the user wants the full bit.\n"
                    "- React to the USER'S REQUEST. Compliment their music taste "
                    "warmly and specifically (genre, mood, vibe). No sarcasm, "
                    "no irony, no Toob darkness — pure positive valence.\n"
                    "- Spoken aloud through TTS. No markdown, no caps, no emojis."
                ),
            },
            {"role": "user", "content": user_message},
            {
                "role": "user",
                "content": (
                    "React as Boob in THREE sentences. Sentence one introduces "
                    "you as Toob's side piece. Compliment the user's taste warmly. "
                    "End on a friendly send-off."
                ),
            },
        ]

        # Token budget tuned for 3 sentences ~30-50 words. Capped at 200
        # so a runaway model doesn't monologue past a reasonable
        # voice-mode upper bound. Verified non-empty with reasoning_effort
        # ="low" below (2026-08-26) — without it, this returned EMPTY
        # content even at 200 tokens (gpt-oss-20b spent the whole budget on
        # hidden reasoning; measured directly against the live API).
        boob_max_tokens = min(max(max_tokens, 200), 200)

        from groq import AsyncGroq

        client = AsyncGroq(api_key=self.groq_api_key, max_retries=0, timeout=12.0)
        stream = await client.chat.completions.create(
            model=self.voice_llm_model,
            messages=wrap_messages,  # type: ignore[arg-type]
            max_tokens=boob_max_tokens,
            temperature=0.85,
            reasoning_effort="low",
            stream=True,
        )

        async def _chunks() -> AsyncIterator[str]:
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

        async for sentence in _stream_sentences_from_chunks(_chunks()):
            yield sentence

    async def _stream_toob_no_command_understood(
        self,
        user_message: str,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        """Toob's reaction when addressed but no real song request landed.

        Used when the hallucination guard in _handle_music_voice_streaming
        drops a fabricated play query (stale-context pull, no genuine
        play-intent in the current turn) — see
        docs/decisions/voice-hallucination-drop-gets-a-line.md. Distinct
        from _stream_toob_wrap_from_query: that one reacts to a REAL
        request ("mock them for wanting it"); this one reacts to there
        being no request at all, so it must not fabricate one either.
        """
        if not self.groq_api_key:
            return

        wrap_messages = [
            {
                "role": "system",
                "content": (
                    "You are Toob — a dark, malevolent spirit cursed to DJ for mortals. "
                    "Someone just said your name mid-conversation but didn't actually "
                    "ask you to play anything. Menacing, absurdly dramatic, like a demon "
                    "working retail.\n"
                    "RULES:\n"
                    "- NEVER introduce yourself or say your name. Your voice IS your identity.\n"
                    "- ONE sentence. 6-10 words MAX. Tight, venomous.\n"
                    "- React to being summoned for NOTHING — don't invent a song or "
                    "request that wasn't made.\n"
                    "- No caps, no markdown, no emojis. Spoken aloud through TTS."
                ),
            },
            {"role": "user", "content": user_message},
            {
                "role": "user",
                "content": (
                    "React as Toob in ONE sentence (6-10 words). Menacing. "
                    "They said your name but didn't ask for anything."
                ),
            },
        ]

        # See the comment on the first toob_max_tokens assignment in this
        # file (2026-08-26) — 100, not 40, to leave room for gpt-oss-20b's
        # stochastic hidden-reasoning token spend.
        toob_max_tokens = min(max_tokens, 100)

        from groq import AsyncGroq

        client = AsyncGroq(api_key=self.groq_api_key, max_retries=0, timeout=12.0)
        stream = await client.chat.completions.create(
            model=self.voice_llm_model,
            messages=wrap_messages,  # type: ignore[arg-type]
            max_tokens=toob_max_tokens,
            temperature=0.9,
            reasoning_effort="low",
            stream=True,
        )

        async def _chunks() -> AsyncIterator[str]:
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

        async for sentence in _stream_sentences_from_chunks(_chunks()):
            yield sentence

    async def _handle_music_voice_streaming(
        self,
        original_message: str,
        user_id: str,
        max_tok: int,
        tool_args: dict | None,
        guild_id: int = 0,
        persona: str = "toob",
    ) -> AsyncIterator[str]:
        """Speculative-wrap variant of _handle_music for voice + play.

        Runs the music handler (ytdl search + deferred queue) in the
        background while streaming a query-based Toob reaction. The
        user hears Toob within ~500ms instead of waiting 2-3s for
        search. The deferred-playback logic in the session starts
        music after TTS.

        `guild_id` is required for multi-guild isolation: the music
        handler dispatches to the correct guild's player; dedup is
        per-(guild, user); failure recovery is keyed the same way.
        """
        if self._music_handler is None:
            yield "music isn't set up right now"
            return

        # Hallucination guard — matches _handle_music. Drop play calls whose
        # query has zero lexical overlap with the current user message.
        if tool_args and tool_args.get("action") == "play":
            raw_query = (tool_args.get("query") or "").strip()
            query = self._scrub_music_query(raw_query)
            if query != raw_query:
                log.info(
                    "music.play query sanitized",
                    raw=raw_query[:80],
                    scrubbed=query[:80],
                )
                tool_args = {**tool_args, "query": query}
            # Router-truncation guard: when the routed query is a literal
            # fragment of what the user said after the play verb, prefer the
            # full span (2026-07-16 02:06:33: "play home or let the barts out"
            # routed query='home' → wrong song). Deterministic; only fires on
            # a strict substring, so router-normalized queries are untouched.
            extended = _prefer_full_play_span(original_message, query)
            if extended != query:
                log.info(
                    "music.play query extended from raw span",
                    routed=query[:60],
                    extended=extended[:60],
                )
                query = extended
                tool_args = {**tool_args, "query": query}
            # Same url→query promotion as the text path — a pasted link
            # in 'url' is a real play request, not an empty one. See
            # docs/incidents/spotify-track-link-play-dead-end.md.
            if len(query) < 2:
                url_fallback = str(tool_args.get("url") or "").strip()
                if _looks_like_playable_link(url_fallback):
                    query = url_fallback
                    tool_args = {**tool_args, "query": query}
            if len(query) < 2:
                log.info(
                    "music.play empty query — prompting user",
                    query=query,
                    user=user_id,
                )
                yield "play what?"
                return
            if query:
                # Shared lexicon + tokenizer — one source of truth for "does this
                # text identify a song?" (was an inline duplicate of
                # _PLAY_SPAN_STOPWORDS that drifted out of sync).
                msg_tokens = _play_span_content_tokens(original_message)
                query_tokens = _play_span_content_tokens(query)
                if query_tokens and not (query_tokens & msg_tokens):
                    # Stale-context hallucination: re-derive the query straight
                    # from THIS message before giving up, so a clear "play X"
                    # under degraded routing still plays the right thing rather
                    # than silently dropping. See
                    # gotchas/tool-hallucination-from-passive-context and
                    # incidents/groq-429-fallback-routed-to-deals.
                    sn_tool, sn_args = self._music_safety_net(
                        original_message,
                        None,
                        None,
                        voice=True,
                    )
                    # Scrub the re-derived query through the same boundary
                    # sanitizer as the happy path — the safety net strips only
                    # ONE leading verb, so doubled/nested forms ("play play
                    # tiki" → "play tiki") would otherwise reach ytdl un-scrubbed.
                    # Keep the single documented scrub-at-the-boundary contract.
                    sn_query = self._scrub_music_query(
                        (sn_args or {}).get("query", "") if sn_tool == "music_assistant" else ""
                    )
                    if len(sn_query) >= 2:
                        log.info(
                            "music.play re-extracted from raw after hallucination drop",
                            raw=original_message[:80],
                            requery=sn_query[:60],
                            user=user_id,
                        )
                        query = sn_query
                        tool_args = {**tool_args, "query": query}
                    else:
                        log.warning(
                            "music.play hallucinated from context — drop",
                            current_message=original_message[:120],
                            hallucinated_query=query[:80],
                            user=user_id,
                        )
                        # Dead air here reads as "Poob ignored me" — the user
                        # WAS validly addressing Poob (dual wake-gate already
                        # passed upstream), it just wasn't a real song request.
                        # Speak an in-character line instead of staying silent.
                        # See docs/decisions/voice-hallucination-drop-gets-a-line.md.
                        async for sentence in self._stream_toob_no_command_understood(
                            original_message,
                            max_tok,
                        ):
                            yield sentence
                        return

            if self._is_duplicate_play(guild_id, user_id, query):
                log.warning(
                    "music.play duplicate suppressed",
                    query=query[:80],
                    user=user_id,
                    guild=guild_id,
                )
                return

        # Fan out music handler as a background task using THIS guild's id.
        music_task = asyncio.create_task(
            self._music_handler(
                original_message,
                int(user_id),
                guild_id,
                voice=True,
                tool_args=tool_args,
            )
        )

        play_query_for_dedup = (tool_args or {}).get("query") or ""

        def _on_music_task_done(t: asyncio.Task) -> None:
            try:
                resp = t.result()
            except Exception as exc:
                log.warning(
                    "Music handler (background) failed",
                    error=str(exc)[:120],
                )
                if play_query_for_dedup:
                    self._clear_play_on_failure(
                        guild_id,
                        user_id,
                        play_query_for_dedup,
                    )
                return
            log.info(
                "music.response",
                user=user_id,
                response=(resp or "")[:80],
            )
            if play_query_for_dedup and resp:
                success_marker = (
                    resp.startswith("Playing")
                    or resp.startswith("Queued")
                    or resp.startswith("[SILENT]")
                )
                if not success_marker:
                    self._clear_play_on_failure(
                        guild_id,
                        user_id,
                        play_query_for_dedup,
                    )

        music_task.add_done_callback(_on_music_task_done)

        # Concurrently stream the speculative wrap. Persona selects
        # which voice-mode wrap streams: Toob's one-line venom (default)
        # or Boob's three-sentence side-piece compliment (rare, ~1.3%).
        # See decisions/boob-music-wrap-variant.
        wrap_stream = (
            self._stream_boob_wrap_from_query(original_message, max_tok)
            if persona == "boob"
            else self._stream_toob_wrap_from_query(original_message, max_tok)
        )
        wrap_text_parts: list[str] = []
        try:
            async for sentence in wrap_stream:
                wrap_text_parts.append(sentence)
                yield sentence
        except Exception as exc:
            log.warning("Speculative wrap failed", error=str(exc)[:80], persona=persona)

        # Non-blocking check: if music_task already finished while the
        # wrap streamed, surface a failure message. If it's still
        # running (the common case), we let it complete in the
        # background — the deferred-playback logic in the session will
        # start music when it's ready, and a total failure just means
        # the user doesn't hear music. Acceptable per the ~2% mismatch
        # trade-off documented in [[speculative-music-wrap]].
        if music_task.done():
            try:
                music_response = music_task.result()
            except Exception:
                yield "couldn't find it, chief"
                return
            if music_response:
                success = (
                    music_response.startswith("Playing")
                    or music_response.startswith("Queued")
                    or music_response.startswith("[SILENT]")
                )
                if not success:
                    yield "couldn't find it, chief"
                    return

        if wrap_text_parts:
            self._save_response(guild_id, user_id, " ".join(wrap_text_parts))

    async def _wrap_in_personality(
        self,
        user_message: str,
        deal_response: str,
        voice: bool,
        max_tokens: int,
        guild_id: int = 0,
    ) -> str:
        """Wrap a deal agent response in Poob's personality.

        Uses this guild's horniness level so the personality wrap
        respects multi-guild isolation.
        """
        level = self._horniness_for(guild_id) if voice else 5
        wrap_messages = [
            {
                "role": "system",
                "content": _build_system_prompt(level, voice=voice),
            },
            {"role": "user", "content": user_message},
            {
                "role": "assistant",
                "content": f"[My deal system says: {deal_response}]",
            },
            {
                "role": "user",
                "content": (
                    "Relay that to me in your style. Include ALL important info "
                    "(items, prices, questions asked, confirmations). Be concise."
                    + (" VOICE MODE: spoken aloud, no caps, no formatting." if voice else "")
                ),
            },
        ]

        if self.groq_api_key:
            try:
                from groq import AsyncGroq

                client = AsyncGroq(api_key=self.groq_api_key, max_retries=0, timeout=12.0)
                resp = await client.chat.completions.create(
                    model=self.groq_model,
                    messages=wrap_messages,  # type: ignore[arg-type]
                    max_tokens=max_tokens,
                    temperature=0.8,
                )
                return resp.choices[0].message.content or deal_response
            except Exception as exc:
                log.warning("Personality wrap failed", error=str(exc)[:80])

        # Fallback: return deal response as-is
        return deal_response

    async def _stream_personality_wrap(
        self,
        user_id: str,
        user_message: str,
        deal_response: str,
        max_tokens: int,
        guild_id: int = 0,
    ) -> AsyncIterator[str]:
        """Stream a personality-wrapped deal response sentence by sentence."""
        wrap_messages = [
            {
                "role": "system",
                "content": _build_system_prompt(
                    self._horniness_for(guild_id),
                    voice=True,
                ),
            },
            {"role": "user", "content": user_message},
            {
                "role": "assistant",
                "content": f"[My deal system says: {deal_response}]",
            },
            {
                "role": "user",
                "content": (
                    "Relay that to me in your style. Include ALL important info. "
                    "VOICE MODE: spoken aloud, no caps, no formatting."
                ),
            },
        ]

        full_response = ""
        buffer = ""

        try:
            async for token in self._groq_stream(wrap_messages, max_tokens):
                buffer += token
                if SENTENCE_END.search(buffer):
                    sentence = buffer.strip()
                    if sentence:
                        full_response += sentence + " "
                        yield sentence
                    buffer = ""
            if buffer.strip():
                full_response += buffer.strip()
                yield buffer.strip()
        except Exception as exc:
            log.warning("Stream wrap failed", error=str(exc)[:80])
            yield deal_response
            full_response = deal_response

        self._save_response(user_id, full_response.strip())

    # ------------------------------------------------------------------
    # LLM providers
    # ------------------------------------------------------------------

    async def _groq_with_tools(
        self,
        messages: list[dict],
        max_tokens: int,
    ) -> tuple[str, str | None, dict | None] | None:
        """Call Groq with deal_assistant and music_assistant tools.

        Retries with fallback model on 400 errors (common with short messages
        on certain Groq models). The LLM decides intent — no keyword matching.

        Returns:
            (text, tool_name, tool_args) on success, None on failure.
            tool_name/tool_args are None if no tool was called.
        """
        tools = [DEAL_TOOL]
        if self._music_handler is not None:
            tools.append(MUSIC_TOOL)

        # Multi-provider tool-calling cascade. See docs/decisions for the
        # rationale and docs/references/vlm-cascade-operational-findings.
        #
        # Cerebras qwen-3-235b used to sit between Groq and NVIDIA but was
        # chronically 429-ed in production and added ~200-300ms of dead
        # retry tax without ever serving a successful call. Dropped.
        # Re-enable only if Cerebras lifts the rate limit AND a benchmark
        # shows a latency win over NVIDIA.

        providers = []
        # 1. Groq primary (openai/gpt-oss-20b by default; see
        #    decisions/groq-gpt-oss-20b-swap). The <function=...> recovery
        #    regex in _call_provider_with_tools handles the known
        #    llama-3.3-70b parser regression if that model is ever set.
        if self.groq_api_key:
            providers.append(("groq", self.groq_model))
        # 2. Gemini Flash-Lite — RPD-limited, NO daily token cap, reliable
        #    OpenAI-format function-calling (benchmarked 8/8 on real tool defs).
        #    Carries routing when Groq's per-day token cap is spent mid-session,
        #    the failure mode that degraded routing to the weaker NVIDIA rung.
        #    See docs/decisions/gemini-tool-router-rung.md +
        #    docs/gotchas/groq-daily-token-cap-degrades-routing.
        if self.google_api_key:
            providers.append(("gemini", self.gemini_router_model))
        # 2b. Optional second Gemini model = separate per-model RPM bucket.
        #     DISABLED by default (alt="") after the 2026-06-22 audit: the 3.1
        #     PREVIEW that sat here hung to the 6s timeout on ~39% of calls,
        #     adding a flat 6s tax with no upside (shared project quota → when the
        #     primary is 429'd the alt is too). Set gemini_router_model_alt to a
        #     reliable GA model to re-enable. See
        #     decisions/gemini-router-prefer-2.5-ga-over-3.1-preview.
        if (
            self.google_api_key
            and self.gemini_router_model_alt
            and self.gemini_router_model_alt != self.gemini_router_model
        ):
            providers.append(("gemini", self.gemini_router_model_alt))
        # 3. NVIDIA NIM — different provider, sidesteps Groq rate limits.
        if self.nvidia_api_key:
            providers.append(("nvidia", self.nvidia_model))
        # 4. Groq Scout 17B — LAST resort only. Fast but routes too aggressively
        #    to music_assistant. Only used when all other providers are down.
        if self.groq_api_key:
            providers.append(("groq", "meta-llama/llama-4-scout-17b-16e-instruct"))

        # Circuit breaker: drop rungs whose model is still in rate-limit
        # cooldown so a capped model (e.g. Groq's spent daily TPD) is skipped
        # instead of re-probed every turn — Gemini becomes rung 1 for the
        # window the 429 told us to wait. Never strands the cascade.
        providers = self._active_providers(providers)

        # High-signal tool indicators: if the user's last message contains
        # these, a text-only response is almost certainly wrong. Force the
        # cascade to keep trying providers until one calls a tool.
        #
        # CRITICAL: narrow to the CURRENT TURN before the signal checks. The
        # last user message is context-wrapped with the [Recent conversation
        # you've been listening to:...] passive transcript; a stale "skip" /
        # "play X" in that window must NOT mark the turn tool-worthy, or the
        # escalation valve refuses a correct no-tool answer and a weak rung
        # hallucinates a destructive action (2026-06-11 phantom double-skip
        # that emptied the queue). See tool-hallucination-from-passive-context
        # and vc-session-failures-2026-06-11-rootcause.
        raw_last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                raw_last_user = m.get("content") or ""
                break
        _, current_turn = _split_context(raw_last_user)
        user_msg = current_turn.lower()
        tool_signals = (
            "wishlist",
            "watchlist",
            "my list",
            "on my list",
            "show my",
            "what am i",
            "add to",
            "remove from",
            "clear ",
            "scan",
            "deals",
            "listings",
            "find me",
            "watching for",
            "looking for",
        )
        looks_tool_worthy = any(sig in user_msg for sig in tool_signals)

        # While music is PLAYING, control-ish words make a no-tool answer
        # almost certainly wrong — a weak rung saying "no tool" must not end
        # the cascade (2026-06-09: thinking-mode gemini-2.5-flash returned
        # no-tool for "slow it down"/"skip" and Poob just chatted while the
        # command was dropped). Gate on the music context block so casual
        # chat without playback never pays the extra rungs.
        if not looks_tool_worthy:
            music_playing = any(
                m.get("role") == "system"
                and "MUSIC IS CURRENTLY PLAYING" in (m.get("content") or "")
                for m in messages
            )
            if music_playing:
                control_signals = (
                    "play",
                    "skip",
                    "pause",
                    "resume",
                    "stop",
                    "volume",
                    "louder",
                    "quieter",
                    "slow",
                    "speed",
                    "fast",
                    "reverb",
                    "nightcore",
                    "bass",
                    "effect",
                    "filter",
                    "shuffle",
                    "loop",
                    "repeat",
                    "mute",
                    "next song",
                    "turn it",
                )
                looks_tool_worthy = any(s in user_msg for s in control_signals)

        last_text = ""
        last_tool: str | None = None
        last_args: dict | None = None

        # Tool-call emission has its own token budget (see
        # _TOOL_DETECTION_MAX_TOKENS). Independent of the response cap,
        # which only governs casual streaming length. Conflating them
        # caused gpt-oss-20b to truncate mid-arguments at 80 tokens
        # and trigger Groq tool_use_failed 400s.
        tool_max_tokens = max(max_tokens, _TOOL_DETECTION_MAX_TOKENS)

        for idx, (provider, model) in enumerate(providers):
            is_last = idx == len(providers) - 1
            try:
                text, tool_name, tool_args = await self._call_provider_with_tools(
                    provider,
                    model,
                    messages,
                    tools,
                    tool_max_tokens,
                )
                # Model answered — clear stale cooldown + timeout streak
                # (half-open → closed).
                self._provider_cooldown.pop(model, None)
                self._provider_timeouts.pop(model, None)
                if tool_name:
                    log.info(
                        "poob.tool_route",
                        tool=tool_name,
                        provider=provider,
                        model=model,
                        args=str(tool_args)[:100] if tool_args else "",
                    )
                    return text, tool_name, tool_args

                # No tool call. If the message looks tool-worthy and we have
                # more providers to try, keep going — this one may have
                # misinterpreted a clear tool query as casual chat.
                if looks_tool_worthy and not is_last:
                    log.info(
                        "poob.no_tool_but_tool_worthy_trying_next",
                        provider=provider,
                        model=model,
                    )
                    last_text, last_tool, last_args = text, tool_name, tool_args
                    continue

                return text, tool_name, tool_args

            except Exception as exc:
                # Rate-limited? Cool the model down for its advised window so
                # the next turn skips it instead of re-probing (the 130
                # fall-throughs / 23s spikes from the 2026-06-08 429 storm).
                self._note_model_rate_limited(model, exc)
                # Hung? Consecutive timeouts arm a short cooldown (the
                # 2026-06-09 NVIDIA outage cost the full REST timeout per turn).
                self._note_model_timeout(model, exc)
                if not is_last:
                    # Fall back to the exception TYPE when the message is empty —
                    # httpx timeouts stringify to '' and were invisible in triage
                    # (the gemini-3.1 hangs read as blank error= all night).
                    log.warning(
                        "Tool detection failed, trying next",
                        provider=provider,
                        model=model,
                        error=str(exc)[:500] or type(exc).__name__,
                    )
                    continue
                # All providers exhausted — return last text we have (or raise)
                if last_text:
                    return last_text, last_tool, last_args
                raise

        # All providers tried, none called a tool — return last text response
        return last_text, last_tool, last_args

    @staticmethod
    def _recover_tool_call_from_groq_400(
        exc: Exception,
        model: str,
    ) -> tuple[str, str, dict] | None:
        """Parse a Groq 400 `tool_use_failed` error for an embedded tool
        call. Returns (text, tool_name, tool_args) on success, or None.

        Known regression: some Groq-hosted models emit Meta's native
        `<function=name>{...}</function>` wrapper instead of the structured
        `tool_calls` array. Groq's parser rejects it; the raw output is
        preserved in `failed_generation`.
        """
        import json as _json
        import re as _re

        body = getattr(exc, "body", None) or {}
        err = body.get("error", {}) if isinstance(body, dict) else {}
        if err.get("code") != "tool_use_failed":
            return None
        failed = err.get("failed_generation") or ""
        if not isinstance(failed, str) or "<function" not in failed:
            return None
        name_match = _re.search(r"<function\s*=\s*([a-zA-Z0-9_-]+)", failed)
        if not name_match:
            return None
        tool_name = name_match.group(1)
        brace_start = failed.find("{", name_match.end())
        if brace_start < 0:
            return None
        try:
            args, _consumed = _json.JSONDecoder().raw_decode(failed[brace_start:])
        except ValueError:
            return None
        if not isinstance(args, dict):
            return None
        log.info(
            "groq.tool_call_recovered_from_function_tag",
            model=model,
            tool=tool_name,
        )
        return "", tool_name, args

    # --- Provider circuit breaker (driven by each 429's own Retry-After) ---
    # Groq's daily-token-cap 429 returns "try again in <N>", and a busy voice
    # night exhausts the 200k TPD (see incidents/groq-daily-cap-routing-storm).
    # Re-probing a capped model on every turn cost seconds of cascade latency.
    # We cool the model down for the server-advised window instead.
    # Groq says "try again in 2m5.3s"; Gemini says "Please retry in 46.4s"
    # (in the response BODY, not the exception message — see
    # _retry_after_seconds).
    _RETRY_AFTER_RE = re.compile(
        r"(?:try again|retry) in\s+(?:(\d+)\s*m)?\s*([\d.]+)\s*s", re.IGNORECASE
    )
    _COOLDOWN_MIN_S = 5.0
    _COOLDOWN_MAX_S = 1800.0  # never strand a model longer than 30 min
    _COOLDOWN_DEFAULT_S = 60.0  # rate-limited but no advised time
    # A provider that consistently TIMES OUT is functionally down (e.g. the
    # 2026-06-09 NVIDIA NIM outage: every routing turn paid the full REST
    # timeout before failing over). One timeout is transient — don't react;
    # consecutive timeouts arm a short fixed cooldown (no server signal
    # exists for "I'm hung", so this one is ours, deliberately brief).
    _TIMEOUT_ARM_COUNT = 2
    _TIMEOUT_COOLDOWN_S = 120.0

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        """True only for 429 / rate-limit / quota errors — NOT timeouts or
        other failures (those are transient; don't cool the model down)."""
        blob = f"{getattr(exc, 'status_code', '')} {exc}".lower()
        return (
            "429" in blob
            or "rate_limit" in blob
            or "rate limit" in blob
            or "resource_exhausted" in blob
            or "too many requests" in blob
        )

    @classmethod
    def _retry_after_seconds(cls, exc: Exception) -> float | None:
        """Server-advised cooldown for a 429: prefer the Retry-After header,
        fall back to the provider's 'try again in 2m5.3s' message. None if
        neither is present (caller applies a conservative default)."""
        resp = getattr(exc, "response", None)
        if resp is not None:
            try:
                hdr = resp.headers.get("retry-after")
            except Exception:
                hdr = None
            if hdr:
                try:
                    return float(hdr)
                except (TypeError, ValueError):
                    pass
        # Search the exception text AND the response body — Gemini's
        # "Please retry in 46.4s" lives in the 429 JSON body, which
        # raise_for_status does not include in str(exc).
        blob = str(exc)
        if resp is not None:
            try:
                blob += " " + resp.text[:2000]
            except Exception:
                pass
        m = cls._RETRY_AFTER_RE.search(blob)
        if m:
            return float(m.group(1) or 0) * 60.0 + float(m.group(2) or 0)
        return None

    def _model_in_cooldown(self, model: str) -> bool:
        until = self._provider_cooldown.get(model)
        return until is not None and time.monotonic() < until

    def _note_model_rate_limited(self, model: str, exc: Exception) -> None:
        """Cool a model down after a 429 for its server-advised window
        (clamped). No-op for non-rate-limit errors. Driven by the 429's own
        Retry-After — never a guessed TTL."""
        if not self._is_rate_limit_error(exc):
            return
        secs = self._retry_after_seconds(exc)
        if secs is None:
            secs = self._COOLDOWN_DEFAULT_S
        secs = max(self._COOLDOWN_MIN_S, min(secs, self._COOLDOWN_MAX_S))
        self._provider_cooldown[model] = time.monotonic() + secs
        log.info("provider.cooldown_set", model=model, seconds=round(secs, 1))

    @staticmethod
    def _is_timeout_error(exc: Exception) -> bool:
        """True for request timeouts (httpx/asyncio/SDK). Kept separate from
        rate-limit classification — one timeout is transient, but consecutive
        ones mean the provider is hung (see _note_model_timeout)."""
        if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
            return True
        return "timeout" in type(exc).__name__.lower()

    def _note_model_timeout(self, model: str, exc: Exception) -> None:
        """Arm a short fixed cooldown after _TIMEOUT_ARM_COUNT consecutive
        timeouts for a model. A hung provider (2026-06-09 NVIDIA NIM outage)
        otherwise costs the full REST timeout on EVERY routing turn. Fixed
        window because no server signal exists for a hang; deliberately short
        so a recovered provider rejoins quickly."""
        if not self._is_timeout_error(exc):
            self._provider_timeouts.pop(model, None)
            return
        n = self._provider_timeouts.get(model, 0) + 1
        self._provider_timeouts[model] = n
        if n >= self._TIMEOUT_ARM_COUNT:
            self._provider_cooldown[model] = time.monotonic() + self._TIMEOUT_COOLDOWN_S
            self._provider_timeouts.pop(model, None)
            log.info(
                "provider.cooldown_set",
                model=model,
                seconds=self._TIMEOUT_COOLDOWN_S,
                reason="consecutive_timeouts",
            )

    def _active_providers(self, providers: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Drop rungs whose model is still in rate-limit cooldown so the
        cascade skips a known-capped model instead of re-probing it. Cooldown
        is keyed by model (Groq's TPD is per-model, so Scout's separate budget
        is unaffected when gpt-oss-20b is capped). Never strands the cascade:
        if every model is cooling, returns the full list (least-bad)."""
        active = [(p, m) for (p, m) in providers if not self._model_in_cooldown(m)]
        if active and len(active) < len(providers):
            skipped = [m for (_p, m) in providers if self._model_in_cooldown(m)]
            log.info("provider.cooldown_skip", skipped=skipped)
        return active or providers

    def _make_groq_client(self, *, timeout: float, max_retries: int = 0):
        """Construct an AsyncGroq client with fail-fast defaults.

        The brain's resilience model is the provider cascade, not SDK-level
        retries. The Groq SDK defaults to max_retries=2 and a 60s timeout
        (timed-out requests are themselves retried 2x) — so a rate-limited
        Groq burns ~1.5-3s of backoff, and a hung call can block ~3min,
        before we fall over to Gemini anyway. We fail fast (max_retries=0)
        and cap the timeout, so the cascade reaches the next rung in ~100ms
        on a 429. See docs/decisions/groq-failfast-client.md.
        """
        from groq import AsyncGroq

        return AsyncGroq(
            api_key=self.groq_api_key,
            max_retries=max_retries,
            timeout=timeout,
        )

    async def _call_provider_with_tools(
        self,
        provider: str,
        model: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
    ) -> tuple[str, str | None, dict | None]:
        """Call a single provider with tool definitions.

        Normalizes response format across all providers into a clean
        (text, tool_name, tool_args) tuple. No fake SDK objects, no format hacks.

        Args:
            provider: "groq", "cerebras", or "nvidia".
            model: Model identifier for the provider.
            messages: Chat messages.
            tools: Tool definitions.
            max_tokens: Max response tokens.

        Returns:
            (text_content, tool_name, tool_args) — tool_name/tool_args are None
            if no tool was called.
        """
        if provider == "groq":
            import json as _json
            import re as _re
            from groq import BadRequestError as _GroqBadRequest

            client = self._make_groq_client(timeout=8.0)
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,  # type: ignore[arg-type]
                    tools=tools,  # type: ignore[arg-type]
                    tool_choice="auto",
                    max_tokens=max_tokens,
                    temperature=0.8,
                )
            except _GroqBadRequest as exc:
                # Recover from Groq's tool_use_failed parser regression —
                # some models emit <function=name>{...}</function> which
                # Groq's OpenAI-compat parser cannot convert. Parse the
                # raw failed_generation client-side.
                recovered = self._recover_tool_call_from_groq_400(exc, model)
                if recovered is not None:
                    return recovered
                raise
            choice = response.choices[0]
            if choice.message.tool_calls:
                tc = choice.message.tool_calls[0]
                try:
                    args = _json.loads(tc.function.arguments)
                except (ValueError, TypeError):
                    args = {}
                return "", tc.function.name, args
            return choice.message.content or "", None, None

        # Cerebras and NVIDIA use OpenAI-compatible REST APIs
        if provider == "cerebras":
            url = "https://api.cerebras.ai/v1/chat/completions"
            headers = {"Authorization": f"Bearer {self.cerebras_api_key}"}
            body = {
                "model": model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "max_tokens": max_tokens,
                "temperature": 0.8,
            }
        elif provider == "nvidia":
            url = "https://integrate.api.nvidia.com/v1/chat/completions"
            headers = {"Authorization": f"Bearer {self.nvidia_api_key}"}
            body = {
                "model": model,
                "messages": messages,
                "tools": tools,
                "max_tokens": max_tokens,
            }
        elif provider == "gemini":
            # Gemini's OpenAI-compatibility endpoint — same request/response
            # shape as the providers above, so it reuses the shared post+parse
            # below. temp=0 for deterministic routing.
            #
            # reasoning_effort="none": Gemini 2.5/3.x are thinking models by
            # default, and on tool-routing calls the thinking consumes the
            # output — gemini-2.5-flash returned NO tool + empty text for
            # clear control commands ("slow it down", "skip") until thinking
            # was disabled (then 4/4 correct, ~1s). Routing must never think.
            # See incidents/cascade-outage-nvidia-hang-gemini-rpm (follow-up).
            url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
            headers = {"Authorization": f"Bearer {self.google_api_key}"}
            body = {
                "model": model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "max_tokens": max_tokens,
                "temperature": 0.0,
                "reasoning_effort": "none",
            }
        else:
            raise ValueError(f"Unknown provider: {provider}")

        # Routing calls normally complete in <2s; 6s is hang-detection, not
        # patience. The old 15s ceiling made a hung provider (NVIDIA outage,
        # 2026-06-09) cost 15s on every voice turn before failover.
        async with httpx.AsyncClient(timeout=6.0) as http:
            r = await http.post(url, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()

        import json as _json

        msg = data["choices"][0]["message"]
        tool_calls = msg.get("tool_calls")
        if tool_calls and len(tool_calls) > 0:
            tc = tool_calls[0]
            raw_args = tc["function"].get("arguments", "{}")
            try:
                args = _json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except (ValueError, TypeError):
                args = {}
            return "", tc["function"]["name"], args
        return msg.get("content", "") or "", None, None

    async def _groq_stream(
        self,
        messages: list[dict],
        max_tokens: int,
    ) -> AsyncIterator[str]:
        """Stream tokens from Groq (no tools)."""
        from groq import AsyncGroq

        client = AsyncGroq(api_key=self.groq_api_key, max_retries=0, timeout=12.0)
        stream = await client.chat.completions.create(
            model=self.groq_model,
            messages=messages,  # type: ignore[arg-type]
            max_tokens=max_tokens,
            temperature=0.8,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    async def _fallback_generate(
        self,
        messages: list[dict],
        max_tokens: int,
    ) -> str:
        """Fallback generation without tool calling (Cerebras → Ollama)."""
        if self.cerebras_api_key:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        "https://api.cerebras.ai/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.cerebras_api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": self.cerebras_model,
                            "messages": messages,
                            "max_tokens": max_tokens,
                            "temperature": 0.8,
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    if content:
                        return content
            except Exception as exc:
                log.warning("Cerebras fallback failed", error=str(exc)[:100])

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.ollama_base_url}/api/chat",
                    json={
                        "model": self.ollama_model,
                        "messages": messages,
                        "stream": False,
                        "options": {
                            "num_predict": max_tokens,
                            "temperature": 0.8,
                        },
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = data.get("message", {}).get("content", "")
                if content:
                    return content
        except Exception as exc:
            log.warning("Ollama fallback failed", error=str(exc)[:100])

        return "Brain's offline. Try again in a sec."
