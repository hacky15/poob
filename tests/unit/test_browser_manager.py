"""Unit tests for BrowserManager.

Focused on the static singleton-lock cleanup and get_page retry logic —
the live browser start path cannot be meaningfully tested without a real
Chromium process.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

    def test_removes_per_pid_lock_files(self, tmp_path: Path) -> None:
        """The .org.chromium.Chromium.XXXXXX per-PID files are deleted."""
        for suffix in ("1aNYCR", "2nCrvN", "8dqWG7"):
            (tmp_path / f".org.chromium.Chromium.{suffix}").write_text("pid-lock")
        # An unrelated dotfile must NOT be deleted.
        (tmp_path / ".config").write_text("keep")

        BrowserManager._cleanup_stale_singleton_locks(tmp_path)

        assert not list(tmp_path.glob(".org.chromium.Chromium.*"))
        assert (tmp_path / ".config").exists()

    def test_removes_browser_metrics_telemetry(self, tmp_path: Path) -> None:
        """BrowserMetrics + DeferredBrowserMetrics directories are deleted."""
        metrics = tmp_path / "BrowserMetrics"
        metrics.mkdir()
        (metrics / "f1.pma").write_text("a" * 1024)
        (metrics / "f2.pma").write_text("b" * 2048)
        deferred = tmp_path / "DeferredBrowserMetrics"
        deferred.mkdir()
        (deferred / "f3.pma").write_text("c" * 512)
        # Real profile data must NOT be touched.
        (tmp_path / "Cookies").write_text("real cookie data")

        BrowserManager._cleanup_stale_singleton_locks(tmp_path)

        assert not metrics.exists()
        assert not deferred.exists()
        assert (tmp_path / "Cookies").read_text() == "real cookie data"


class TestGetPageRetries:
    """get_page() must materialize a tab and retry through CDP-not-ready."""

    @pytest.fixture
    def manager(self) -> BrowserManager:
        return BrowserManager(headless=True)

    @pytest.mark.asyncio
    async def test_returns_current_page_when_available(
        self, manager: BrowserManager,
    ) -> None:
        existing_page = MagicMock(name="existing_page")
        manager._browser = MagicMock()
        manager._browser.get_current_page = AsyncMock(return_value=existing_page)
        manager._browser.new_page = AsyncMock()

        result = await manager.get_page()

        assert result is existing_page
        manager._browser.new_page.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_new_page_when_none_active(
        self, manager: BrowserManager,
    ) -> None:
        created_page = MagicMock(name="created_page")
        manager._browser = MagicMock()
        manager._browser.get_current_page = AsyncMock(return_value=None)
        manager._browser.new_page = AsyncMock(return_value=created_page)

        result = await manager.get_page()

        assert result is created_page
        manager._browser.new_page.assert_called_once()

    @pytest.mark.asyncio
    async def test_retries_when_cdp_not_initialized(
        self, manager: BrowserManager, monkeypatch,
    ) -> None:
        """new_page() raising 'CDP client not initialized' triggers retry."""
        # Patch asyncio.sleep so the test doesn't actually wait 30s.
        sleeps: list[float] = []

        async def _no_sleep(delay: float) -> None:
            sleeps.append(delay)

        monkeypatch.setattr("asyncio.sleep", _no_sleep)

        created_page = MagicMock(name="created_page")
        # First two attempts fail with the retryable error; third succeeds.
        new_page_mock = AsyncMock(
            side_effect=[
                RuntimeError("CDP client not initialized - browser may not be connected yet"),
                RuntimeError("CDP client not initialized - browser may not be connected yet"),
                created_page,
            ],
        )
        manager._browser = MagicMock()
        manager._browser.get_current_page = AsyncMock(return_value=None)
        manager._browser.new_page = new_page_mock

        result = await manager.get_page()

        assert result is created_page
        assert new_page_mock.call_count == 3
        assert len(sleeps) == 2

    @pytest.mark.asyncio
    async def test_non_retryable_error_raises_immediately(
        self, manager: BrowserManager, monkeypatch,
    ) -> None:
        """A different exception (not CDP-not-initialized) should NOT retry."""
        sleeps: list[float] = []

        async def _no_sleep(delay: float) -> None:
            sleeps.append(delay)

        monkeypatch.setattr("asyncio.sleep", _no_sleep)

        new_page_mock = AsyncMock(
            side_effect=RuntimeError("Some other unrelated failure"),
        )
        manager._browser = MagicMock()
        manager._browser.get_current_page = AsyncMock(return_value=None)
        manager._browser.new_page = new_page_mock

        with pytest.raises(RuntimeError, match="non-retryable error"):
            await manager.get_page()

        # Only one call — no retry on non-retryable errors.
        new_page_mock.assert_called_once()
        assert sleeps == []

    @pytest.mark.asyncio
    async def test_exhausts_retries_then_raises(
        self, manager: BrowserManager, monkeypatch,
    ) -> None:
        """If CDP never connects, get_page raises after the retry budget."""
        async def _no_sleep(delay: float) -> None:
            pass

        monkeypatch.setattr("asyncio.sleep", _no_sleep)

        new_page_mock = AsyncMock(
            side_effect=RuntimeError("CDP client not initialized - browser may not be connected yet"),
        )
        manager._browser = MagicMock()
        manager._browser.get_current_page = AsyncMock(return_value=None)
        manager._browser.new_page = new_page_mock

        with pytest.raises(RuntimeError, match="failed after"):
            await manager.get_page()

        # 6 attempts total per the implementation.
        assert new_page_mock.call_count == 6

    @pytest.mark.asyncio
    async def test_short_circuits_after_max_consecutive_failures(
        self, manager: BrowserManager, monkeypatch,
    ) -> None:
        """After N consecutive CDP failures, subsequent calls fail fast."""
        async def _no_sleep(delay: float) -> None:
            pass

        monkeypatch.setattr("asyncio.sleep", _no_sleep)

        new_page_mock = AsyncMock(
            side_effect=RuntimeError("CDP client not initialized - browser may not be connected yet"),
        )
        manager._browser = MagicMock()
        manager._browser.get_current_page = AsyncMock(return_value=None)
        manager._browser.new_page = new_page_mock
        manager._max_consecutive_cdp_failures = 2  # Speed up for test

        # First failure: full retry budget consumed (6 attempts).
        with pytest.raises(RuntimeError, match="failed after"):
            await manager.get_page()
        assert new_page_mock.call_count == 6
        assert not manager._cdp_permanently_broken

        # Second failure: another full retry budget. After this, threshold met.
        with pytest.raises(RuntimeError, match="failed after"):
            await manager.get_page()
        assert new_page_mock.call_count == 12
        assert manager._cdp_permanently_broken  # Now flipped

        # Third call: short-circuits IMMEDIATELY — no more new_page() calls.
        with pytest.raises(RuntimeError, match="permanently broken"):
            await manager.get_page()
        assert new_page_mock.call_count == 12  # Unchanged

    @pytest.mark.asyncio
    async def test_success_resets_consecutive_failure_count(
        self, manager: BrowserManager, monkeypatch,
    ) -> None:
        """A successful get_page() clears the consecutive-failure counter."""
        async def _no_sleep(delay: float) -> None:
            pass

        monkeypatch.setattr("asyncio.sleep", _no_sleep)

        created_page = MagicMock(name="created_page")
        manager._browser = MagicMock()
        manager._browser.get_current_page = AsyncMock(return_value=None)
        manager._browser.new_page = AsyncMock(return_value=created_page)
        manager._consecutive_cdp_failures = 2  # Pretend we've failed twice

        result = await manager.get_page()

        assert result is created_page
        assert manager._consecutive_cdp_failures == 0  # Reset
        assert not manager._cdp_permanently_broken


class TestHeadlessAutoDetect:
    """Headful Chromium cannot launch without an X display, so headless=False
    in a displayless container must be auto-forced to headless — the root
    cause of the authenticated main browser being dead in prod."""

    def test_forces_headless_when_no_display(self, monkeypatch):
        monkeypatch.delenv("DISPLAY", raising=False)
        mgr = BrowserManager(headless=False)
        assert mgr._headless is True

    def test_respects_headful_when_display_present(self, monkeypatch):
        monkeypatch.setenv("DISPLAY", ":0")
        mgr = BrowserManager(headless=False)
        assert mgr._headless is False

    def test_headless_true_stays_true_regardless(self, monkeypatch):
        monkeypatch.delenv("DISPLAY", raising=False)
        assert BrowserManager(headless=True)._headless is True
        monkeypatch.setenv("DISPLAY", ":0")
        assert BrowserManager(headless=True)._headless is True
