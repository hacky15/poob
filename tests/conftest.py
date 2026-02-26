"""Shared test fixtures for the agentic scraper test suite."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from agentic_scraper.storage.models import Deal, DealScore, Listing, ScanLog, WatchItem


@pytest.fixture
def app_config(tmp_path: Path):
    """Config with test defaults, database in tmp_path."""
    from agentic_scraper.config import AppConfig

    return AppConfig(
        _env_file=None,
        discord_bot_token="test-token-123",
        discord_deals_channel_id=123456789,
        database_path=tmp_path / "test.db",
        log_dir=tmp_path / "logs",
        ollama_base_url="http://localhost:11434",
        ollama_model="qwen3:8b",
    )


@pytest.fixture
async def db_connection(tmp_path: Path):
    """In-memory SQLite with schema initialized."""
    from agentic_scraper.storage.database import init_schema

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await init_schema(conn)
    yield conn
    await conn.close()


@pytest.fixture
def mock_llm():
    """Mock LangChain chat model that returns predictable responses."""
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=MagicMock(content='{"result": "test"}'))
    return llm


@pytest.fixture
def mock_browser_manager():
    """Mock BrowserManager that doesn't launch a real browser."""
    manager = AsyncMock()
    agent_mock = AsyncMock()
    agent_mock.run = AsyncMock(
        return_value=MagicMock(
            final_result=MagicMock(return_value="[]"),
            is_successful=MagicMock(return_value=True),
        )
    )
    manager.create_agent.return_value = agent_mock
    return manager


@pytest.fixture
def sample_listing() -> Listing:
    """A realistic sample marketplace listing."""
    return Listing(
        site="facebook_marketplace",
        external_id="fb_12345",
        title="PlayStation 5 Disc Edition",
        price=250.00,
        description="Like new, barely used PS5 disc edition with controller",
        location="Portland, OR",
        seller_name="John D.",
        listing_url="https://facebook.com/marketplace/item/12345",
        image_urls=["https://example.com/ps5.jpg"],
    )


@pytest.fixture
def sample_watch_item() -> WatchItem:
    """A realistic sample watch item."""
    return WatchItem(
        keywords="PS5",
        max_price=300.00,
        location="Portland, OR",
        radius_miles=25,
        discord_user_id="user_001",
        discord_channel_id="channel_001",
    )


@pytest.fixture
def sample_deal() -> Deal:
    """A realistic sample deal."""
    return Deal(
        listing_id="listing_001",
        watch_item_id="watch_001",
        score=DealScore.GREAT,
        estimated_market_price=400.00,
        discount_pct=37.5,
        llm_reasoning="PS5 Disc Edition typically sells for $400. This is priced at $250.",
    )


@pytest.fixture
def sample_scan_log() -> ScanLog:
    """A realistic sample scan log."""
    return ScanLog(
        site="facebook_marketplace",
        query_keywords="PS5",
        listings_found=15,
        deals_found=2,
        duration_seconds=45.3,
    )
