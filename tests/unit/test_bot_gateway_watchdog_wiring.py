"""Structural guard: ScraperBot must actually start its GatewayWatchdog.

A behavioral voice/text test can't see "a background watchdog thread never
got started" -- the bot looks and behaves identically either way until the
gateway wedges hours later in production, exactly the shape of bug this
project has been bitten by before (see docs/incidents/
voice-llm-model-deprecated-and-never-wired.md: a config field that existed
but was never wired into the constructor). This test exists so a future
refactor of on_ready can't silently drop the watchdog.start() call.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from poob.discord_bot.bot import ScraperBot
from poob.discord_bot.gateway_watchdog import GatewayWatchdog


@pytest.mark.asyncio
async def test_constructor_attaches_a_gateway_watchdog(app_config) -> None:
    # Async: py-cord's Client.__init__ calls the deprecated
    # asyncio.get_event_loop() when no loop is passed explicitly. Outside
    # a running loop (this is the only sync test in the suite that builds
    # a ScraperBot), Python 3.13 raises RuntimeError instead of the old
    # create-one-and-warn fallback once some other test has already
    # touched thread-local event-loop state. pytest-asyncio guarantees a
    # running loop for this thread, sidestepping the whole question.
    bot = ScraperBot(config=app_config)
    assert isinstance(bot._gateway_watchdog, GatewayWatchdog)
    assert bot._gateway_watchdog._bot is bot


@pytest.mark.asyncio
async def test_on_ready_starts_the_gateway_watchdog(app_config) -> None:
    bot = ScraperBot(config=app_config)

    # on_ready does a lot more than this (cog loading, slash sync, voice
    # auto-rejoin, owner DM) -- none of it is what this test is about, and
    # exercising it for real would need a live gateway connection. Stub
    # those out so the watchdog-start behavior is isolated and this test
    # can't pass or fail for the wrong reason.
    bot._cogs_loaded = True  # skip _load_cogs / _register_persistent_views / _sync_slash_commands
    bot.notifier = None
    bot.config.discord_owner_user_id = 0  # skip _notify_owner_alive

    with patch.object(GatewayWatchdog, "start") as mock_start:
        await bot.on_ready()

    mock_start.assert_called_once_with()
