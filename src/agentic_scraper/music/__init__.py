"""Music subsystem — YouTube playback with real-time TTS mixing."""

from agentic_scraper.music.queue import LoopMode, MusicQueue, Track, TrackSource
from agentic_scraper.music.player import GuildMusicPlayer, MixingAudioSource
from agentic_scraper.music.ytdl import AsyncYTDL

__all__ = [
    "AsyncYTDL",
    "GuildMusicPlayer",
    "LoopMode",
    "MixingAudioSource",
    "MusicQueue",
    "Track",
    "TrackSource",
]
