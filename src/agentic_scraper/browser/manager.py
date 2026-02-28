"""Browser lifecycle management and agent creation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from browser_use import Agent, BrowserProfile, BrowserSession

from agentic_scraper.utils.logging import get_logger

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

        profile = BrowserProfile(
            headless=self._headless,
            user_data_dir=user_data_dir,
            window_size={"width": 1280, "height": 1100},
            wait_between_actions=self._stealth_min_delay_ms / 1000.0,
        )

        self._browser = BrowserSession(browser_profile=profile)
        await self._browser.start()
        log.info("Browser started", headless=self._headless)

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

        Returns the browser-use Page object from the active BrowserSession,
        enabling direct navigation, JS evaluation, and content extraction
        without creating an LLM Agent.

        Returns:
            A browser-use Page instance.

        Raises:
            RuntimeError: If browser has not been started or no page is active.
        """
        if self._browser is None:
            raise RuntimeError("Browser not started. Call start() first.")
        page = await self._browser.get_current_page()
        if page is None:
            raise RuntimeError("No active page in browser session.")
        return page

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
