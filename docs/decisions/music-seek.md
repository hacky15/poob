---
type: decision
status: active
date: 2026-05-12
tags: [music, seek, brain-tool, ffmpeg]
related: [[music-on-the-fly-filter-respawn]] [[music-queue-primitives]] [[music-filter-presets]] [[music-player-architecture]]
---

# Music seek — multi-format time parser + respawn-based jump

## Context

The roadmap [[music-bot-feature-roadmap]] ranked seek as #7 — cheap (~2h) and high value because the respawn machinery from [[music-on-the-fly-filter-respawn]] already exists. Users expect to say "skip ahead 10 seconds" or "go to 2:30" in voice OR type `/seek 2m30s` in text; every other music bot in the field supports both styles. The cheapest path is a dedicated parser that absorbs the format variations, then a thin ``GuildMusicPlayer.seek`` that reuses the existing respawn.

## Decision

Three pieces, all small:

### 1. ``poob.music.seek`` — time-format parser

```python
parse_seek_input(raw: str) -> ParsedSeek(seconds: float, relative: bool)
```

Supports five input shapes, in priority order:

| Format | Example | Result |
|---|---|---|
| Relative prefix | ``+10``, ``-1m``, ``+1m30s`` | ``relative=True`` |
| Clock | ``2:30``, ``1:02:03`` | absolute, validates ss/mm ≤ 59 |
| Suffixed | ``2m30s``, ``1h5m``, ``45s`` | absolute, no upper bound on minutes |
| Bare seconds | ``150``, ``37`` | absolute |
| (rejected) | ``2:60``, ``the future`` | ``SeekParseError`` |

Case-insensitive (STT capitalizes unpredictably), whitespace-tolerant, rejects negative absolute clock (``-2:30`` is nonsense; negative bare seconds parse as relative).

### 2. ``GuildMusicPlayer.seek(ParsedSeek) -> (Track, float) | None``

Computes the absolute target. Relative offsets add to ``position_seconds`` (the wall-clock + pause-aware tracker from [[music-on-the-fly-filter-respawn]]). Clamps:

- Negative ⇒ 0.0
- Past ``duration - 1.0`` ⇒ ``duration - 1.0`` (seek-past-end is a separate verb, ``skip``)
- No duration metadata ⇒ no upper clamp (let FFmpeg end-of-stream naturally)
- Livestream ⇒ early ``None`` return (the handler surfaces ``Can't seek a live stream``)

Sets ``_respawn_request = (track, target, active_effect_chain)`` and calls ``voice_client.stop()``. The existing inner-respawn loop in ``_player_loop`` rebuilds the FFmpeg source with ``-ss <target>``. The ~200-400 ms audible gap is documented in [[ffmpeg-effect-toggle-creates-audio-gap]].

### 3. ``music_assistant`` action ``seek`` with ``time: str`` param

LLM emits the literal user phrasing (e.g. ``"+10s"``, ``"2:30"``); the handler parses. The schema's ``time`` description lists the accepted formats with natural-language mappings:

- "skip ahead 10 seconds" → ``"+10"``
- "go to two minutes" → ``"2:00"``
- "start over" → use ``replay`` instead

Handler responses (all SILENT):

- Success: ``Seeked {title} to {m:ss}.``
- Missing ``time``: ``Where to? (e.g. '2:30', '+10', '-30s')``
- Parse error: surface the parser's message (includes the format list).
- Livestream: ``Can't seek a live stream.``
- No current track: ``Nothing is playing to seek within.``

## Alternatives considered

- **Inline regex in the handler.** Fine for one format, awful for five. The parser module is reusable from any future entry point (slash command, button callback, voice utterance) and unit-testable without a player.
- **Pass numeric seconds in the tool args.** Forces the LLM to do the arithmetic. The LLM is good at the call ("seek to 2:30") but worse at the math (sometimes emits ``150.0``, sometimes ``150``, sometimes mistakenly converts to ms). Letting the LLM emit the literal user phrasing and parsing on our side is cheaper and more reliable.
- **Add a separate ``seek_forward`` / ``seek_back`` action.** Bloats the enum; the ``time`` param's leading ``+`` / ``-`` already distinguishes them. Single ``seek`` action with a flexible format string mirrors what Rythm and JMusicBot do.
- **Treat past-end-of-track seeks as ``skip``.** Considered. Rejected because the user could be intentionally seeking near the end of a long track — the 1-second-headroom clamp gives them that without crossing into ``skip`` territory.

## Consequences

- One new action, one new schema parameter, one new module, three small chunks of code. No infrastructure changes; no new deps.
- Reuses the respawn machinery completely — same audible-gap profile, same effect-preservation semantics (seeking under nightcore stays nightcored).
- ``time`` is intentionally a string. The LLM is allowed to be permissive ("two minutes thirty", but the LLM should translate that to "2:30"); the parser is strict (rejects "two minutes thirty"). If users find a phrasing the LLM can't translate, we extend the schema description, not the parser.
- ``-ss`` on network streams is not guaranteed to work. If yt-dlp returned a stream URL (download fallback path), seek may start from 0 or fail; FFmpeg's reconnect flags catch most cases. Pre-downloaded local files (the common case for non-livestream tracks) seek instantly.

## Validation

- 36 parser tests in ``tests/unit/test_music_seek_parser.py`` covering each format, clamps, rejections, whitespace, case, and decimal seconds.
- 10 player tests in ``tests/unit/test_music_player_seek.py`` covering absolute / relative / clamps / livestream / no-duration / effect preservation.
- 6 handler tests in ``tests/unit/test_music_handler_actions.py`` covering dispatch, missing arg, parse error, livestream, no-current-track.
- Schema regression guard updated to require ``seek`` in the action enum and ``time`` in the parameter set.

## Rollback

Remove the ``seek`` branch from the handler and the action / ``time`` from the schema. ``parse_seek_input`` and ``GuildMusicPlayer.seek`` can stay; they're inert without a dispatch.
