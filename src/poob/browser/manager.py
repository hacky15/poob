"""Browser lifecycle management and agent creation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from browser_use import Agent, BrowserProfile, BrowserSession

from poob.utils.logging import get_logger

if TYPE_CHECKING:
    from typing import Any

    BaseChatModel = Any  # browser-use 0.12+ uses its own LLM Protocol, not LangChain's

log = get_logger("browser.manager")


class BrowserManager:
    """Manages browser lifecycle and creates ephemeral browser-use Agents.

    The browser session is long-lived (one per session). Agents are
    ephemeral (one per scan task) since they accumulate action history
    and should be discarded after each task completes.

    Args:
        headless: Run browser without visible window.
        profiles_dir: Directory for persistent cookie/profile storage.
        use_vision: Enable screenshot-based vision mode for agents.
        stealth_min_delay_ms: Minimum delay between actions in ms.
        stealth_max_delay_ms: Maximum delay between actions in ms.
    """

    def __init__(
        self,
        *,
        headless: bool = False,
        profiles_dir: Path = Path("browser_profiles"),
        use_vision: bool = True,
        stealth_min_delay_ms: int = 1500,
        stealth_max_delay_ms: int = 4000,
    ) -> None:
        self._headless = headless
        self._profiles_dir = profiles_dir
        self._use_vision = use_vision
        self._stealth_min_delay_ms = stealth_min_delay_ms
        self._stealth_max_delay_ms = stealth_max_delay_ms
        self._browser: BrowserSession | None = None

    async def start(self, cookies_file: str | None = None) -> None:
        """Launch the browser with persistent profile config.

        Args:
            cookies_file: Optional path to a cookies JSON file relative to profiles_dir.
                          Used as user_data_dir for persistent login state.
        """
        user_data_dir = None
        if cookies_file:
            # Use the directory containing the cookies file as user data dir
            profile_path = self._profiles_dir / Path(cookies_file).parent
            profile_path.mkdir(parents=True, exist_ok=True)
            user_data_dir = str(profile_path)
            self._cleanup_stale_singleton_locks(profile_path)

        profile = BrowserProfile(
            headless=self._headless,
            user_data_dir=user_data_dir,
            window_size={"width": 1280, "height": 1100},
            wait_between_actions=self._stealth_min_delay_ms / 1000.0,
        )

        self._browser = BrowserSession(browser_profile=profile)
        await self._browser.start()
        log.info("Browser started", headless=self._headless)

    @staticmethod
    def _cleanup_stale_singleton_locks(profile_path: Path) -> None:
        """Remove Chromium SingletonLock / SingletonCookie / SingletonSocket
        entries left behind by a non-graceful previous shutdown.

        Chromium's profile locking assumes the owning process will `unlink`
        these on exit. When the process is SIGKILLed (container stop, OOM,
        hard kill), the symlinks remain, point at a dead hostname-PID, and
        the next launch hangs in `LocalBrowserWatchdog.on_BrowserLaunchEvent`
        for ~30-45s waiting for the "other" instance to release the profile.

        Removing these before launch is safe: they are pure lock files,
        no user data is stored in them.
        """
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock_path = profile_path / lock_name
            try:
                # Use lexists() to catch broken symlinks — the symlink
                # target often lives in /tmp of a container that's gone.
                if lock_path.is_symlink() or lock_path.exists():
                    lock_path.unlink()
                    log.info("Removed stale Chromium singleton lock", path=str(lock_path))
            except OSError as exc:
                log.warning(
                    "Could not remove stale singleton lock",
                    path=str(lock_path),
                    error=str(exc),
                )

    async def stop(self) -> None:
        """Gracefully close the browser."""
        if self._browser:
            await self._browser.stop()
            self._browser = None
            log.info("Browser stopped")

    def create_agent(
        self,
        task: str,
        llm: BaseChatModel,
        *,
        use_vision: bool | None = None,
        max_actions_per_step: int = 3,
        max_failures: int = 3,
    ) -> Agent:
        """Create an ephemeral browser-use Agent for a specific task.

        Args:
            task: Natural language task description for the agent.
            llm: LangChain chat model to drive the agent.
            use_vision: Override default vision mode. None uses instance default.
            max_actions_per_step: Max actions the agent can take per LLM call.
            max_failures: Max consecutive failures before the agent gives up.

        Returns:
            A configured browser-use Agent ready to run.

        Raises:
            RuntimeError: If browser has not been started.
        """
        if self._browser is None:
            raise RuntimeError("Browser not started. Call start() first.")

        vision = use_vision if use_vision is not None else self._use_vision

        return Agent(
            task=task,
            llm=llm,
            browser_session=self._browser,
            use_vision=vision,
            max_actions_per_step=max_actions_per_step,
            max_failures=max_failures,
        )

    async def get_page(self) -> object:
        """Get the current CDP Page for direct browser control.

        Returns the browser-use Page object from the active BrowserSession.
        If the session has no open tab (browser-use 0.12+ does not auto-open
        one on ``start()``), materializes a fresh tab via ``new_page()`` so
        callers always get a usable Page instead of ``None``.

        Browser-use's ``bubus`` event bus times out its internal launch
        handler at 30s and propagates ``asyncio.TimeoutError`` up — main.py
        catches that and treats startup as failed, but the underlying
        BrowserSession's CDP client may still be in the middle of connecting.
        ``new_page()`` raises "CDP client not initialized" until that
        handshake completes. We retry with backoff so the first patrol cycle
        (~3 min after startup) almost always finds a ready browser.

        Returns:
            A browser-use Page instance.

        Raises:
            RuntimeError: If browser has not been started, or if both
                ``get_current_page()`` and ``new_page()`` fail across all
                retries.
        """
        import asyncio as _asyncio

        if self._browser is None:
            raise RuntimeError("Browser not started. Call start() first.")
        page = await self._browser.get_current_page()
        if page is not None:
            return page

        # No active tab — materialize one. Retry while CDP is still
        # initializing in the background.
        max_attempts = 6
        retry_delay = 5.0  # seconds; total worst case 30s above the bubus race
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                new_page = await self._browser.new_page()
                if new_page is not None:
                    if attempt > 1:
                        log.info(
                            "Main browser tab materialized after retry",
                            attempt=attempt,
                        )
                    else:
                        log.info("No active page in browser session; created a new tab")
                    return new_page
                last_exc = RuntimeError("new_page() returned None")
            except Exception as exc:
                last_exc = exc
                # Only retry on the known "still connecting" failure mode.
                # Anything else is a real bug — surface it immediately.
                if "CDP client not initialized" not in str(exc):
                    raise RuntimeError(
                        f"new_page() failed with non-retryable error: {exc}",
                    ) from exc
                log.info(
                    "Browser CDP not ready, waiting for handshake",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    wait_s=retry_delay,
                )
            await _asyncio.sleep(retry_delay)

        raise RuntimeError(
            f"new_page() failed after {max_attempts} attempts; "
            f"last error: {last_exc}",
        )

    def get_session(self) -> BrowserSession:
        """Get the underlying BrowserSession for CDP access.

        Returns:
            The active BrowserSession instance.

        Raises:
            RuntimeError: If browser has not been started.
        """
        if self._browser is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._browser

    @property
    def is_running(self) -> bool:
        """Whether the browser is currently running."""
        return self._browser is not None

    async def __aenter__(self) -> BrowserManager:
        """Context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        """Context manager exit."""
        await self.stop()
