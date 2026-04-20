"""Music subsystem — YouTube playback with real-time TTS mixing."""

from poob.music.queue import LoopMode, MusicQueue, Track, TrackSource
from poob.music.player import GuildMusicPlayer, MixingAudioSource
from poob.music.ytdl import AsyncYTDL

__all__ = [
    "AsyncYTDL",
    "GuildMusicPlayer",
    "LoopMode",
    "MixingAudioSource",
    "MusicQueue",
    "Track",
    "TrackSource",
]
