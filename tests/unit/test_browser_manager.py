"""Unit tests for BrowserManager.

Focused on the static singleton-lock cleanup — the live browser start path
cannot be meaningfully tested without a real Chromium process.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from poob.browser.manager import BrowserManager


def _can_symlink() -> bool:
    """Windows blocks symlink creation without elevation / developer mode.
    Production runs on Linux docker where this is always available."""
    if sys.platform != "win32":
        return True
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            link = Path(td) / "probe"
            link.symlink_to("target")
            return True
    except OSError:
        return False


_SYMLINKS_AVAILABLE = _can_symlink()
_requires_symlinks = pytest.mark.skipif(
    not _SYMLINKS_AVAILABLE,
    reason="symlink creation requires privilege on this platform",
)


class TestCleanupStaleSingletonLocks:
    """Pre-launch removal of stale Chromium singleton-lock files."""

    def test_removes_regular_lock_files(self, tmp_path: Path) -> None:
        """Plain file locks (non-symlink) are deleted."""
        (tmp_path / "SingletonLock").write_text("pid-42")
        (tmp_path / "SingletonCookie").write_text("cookie-data")
        (tmp_path / "SingletonSocket").write_text("socket-path")
        (tmp_path / "unrelated.txt").write_text("keep me")

        BrowserManager._cleanup_stale_singleton_locks(tmp_path)

        assert not (tmp_path / "SingletonLock").exists()
        assert not (tmp_path / "SingletonCookie").exists()
        assert not (tmp_path / "SingletonSocket").exists()
        assert (tmp_path / "unrelated.txt").exists()

    @_requires_symlinks
    def test_removes_broken_symlinks(self, tmp_path: Path) -> None:
        """Broken symlinks (the real-world case from docker restarts) are deleted."""
        lock = tmp_path / "SingletonLock"
        lock.symlink_to("nonexistent-host-pid")
        cookie = tmp_path / "SingletonCookie"
        cookie.symlink_to("/tmp/gone/cookie")
        socket = tmp_path / "SingletonSocket"
        socket.symlink_to("/tmp/org.chromium.Chromium.ABCD/SingletonSocket")

        assert lock.is_symlink() and not lock.exists()  # Broken symlink

        BrowserManager._cleanup_stale_singleton_locks(tmp_path)

        assert not lock.is_symlink()
        assert not cookie.is_symlink()
        assert not socket.is_symlink()

    def test_no_op_on_clean_directory(self, tmp_path: Path) -> None:
        """Empty profile directory is silently OK."""
        # Should not raise.
        BrowserManager._cleanup_stale_singleton_locks(tmp_path)
        # Directory still usable.
        assert tmp_path.is_dir()

    def test_preserves_real_profile_data(self, tmp_path: Path) -> None:
        """Cookies / Local Storage / extensions are not touched."""
        (tmp_path / "Cookies").write_text("real cookie data")
        (tmp_path / "Local Storage").mkdir()
        (tmp_path / "Local Storage" / "leveldb.log").write_text("leveldb-data")
        (tmp_path / "Default").mkdir()
        (tmp_path / "SingletonLock").write_text("host-42")

        BrowserManager._cleanup_stale_singleton_locks(tmp_path)

        assert (tmp_path / "Cookies").read_text() == "real cookie data"
        assert (tmp_path / "Local Storage" / "leveldb.log").exists()
        assert (tmp_path / "Default").is_dir()
        assert not (tmp_path / "SingletonLock").exists()

    def test_tolerates_permission_errors(self, tmp_path: Path, monkeypatch) -> None:
        """An unlink that raises OSError is logged but does not crash startup."""
        lock = tmp_path / "SingletonLock"
        lock.write_text("data")

        def _raise_unlink(self: Path, *args: object, **kwargs: object) -> None:
            raise OSError("EBUSY mock")

        monkeypatch.setattr(Path, "unlink", _raise_unlink)

        # Should not raise — the whole point is to keep startup alive.
        BrowserManager._cleanup_stale_singleton_locks(tmp_path)
