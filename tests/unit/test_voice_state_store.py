"""Tests for the voice-membership persistence store (auto-rejoin on restart).

Records {guild_id: channel_id} so a redeploy can rejoin. Must survive a
process restart (new store instance reads the same file), be best-effort
(never raise on I/O errors), and round-trip correctly. See
docs/decisions/voice-auto-rejoin-on-restart.md.
"""

from __future__ import annotations

from poob.voice.voice_state_store import VoiceStateStore


def test_record_and_load_roundtrip(tmp_path) -> None:
    store = VoiceStateStore(tmp_path / "voice_state.json")
    store.record(111, 222)
    store.record(333, 444)
    assert sorted(store.load()) == [(111, 222), (333, 444)]


def test_persists_across_instances(tmp_path) -> None:
    path = tmp_path / "voice_state.json"
    VoiceStateStore(path).record(111, 222)
    # New instance (simulates a restart) reads the same file.
    assert VoiceStateStore(path).load() == [(111, 222)]


def test_record_updates_channel_for_guild(tmp_path) -> None:
    store = VoiceStateStore(tmp_path / "s.json")
    store.record(111, 222)
    store.record(111, 999)  # moved channels in the same guild
    assert store.load() == [(111, 999)]


def test_forget_removes_only_that_guild(tmp_path) -> None:
    store = VoiceStateStore(tmp_path / "s.json")
    store.record(111, 222)
    store.record(333, 444)
    store.forget(111)
    assert store.load() == [(333, 444)]


def test_forget_missing_is_noop(tmp_path) -> None:
    store = VoiceStateStore(tmp_path / "s.json")
    store.record(111, 222)
    store.forget(999)  # not present
    assert store.load() == [(111, 222)]


def test_load_on_missing_file_is_empty(tmp_path) -> None:
    assert VoiceStateStore(tmp_path / "nope.json").load() == []


def test_corrupt_file_loads_empty(tmp_path) -> None:
    path = tmp_path / "s.json"
    path.write_text("{ this is not json", encoding="utf-8")
    store = VoiceStateStore(path)
    assert store.load() == []
    # And a subsequent record overwrites the garbage cleanly.
    store.record(111, 222)
    assert store.load() == [(111, 222)]


def test_write_failure_is_swallowed(tmp_path, monkeypatch) -> None:
    # A persistence error must never raise into join/leave/boot.
    store = VoiceStateStore(tmp_path / "s.json")

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(type(store._path), "write_text", _boom, raising=False)
    store.record(111, 222)  # must not raise
