"""Tests for UserPreferencesRepository."""

from __future__ import annotations

import pytest

from poob.storage.repositories.preferences_repo import UserPreferencesRepository


class TestUserPreferencesRepository:
    """CRUD tests for user preferences using in-memory SQLite."""

    async def test_set_and_get(self, db_connection):
        repo = UserPreferencesRepository(db_connection)
        await repo.set("user_1", "location", '{"city": "Portland, OR"}')

        result = await repo.get("user_1", "location")
        assert result == '{"city": "Portland, OR"}'

    async def test_get_nonexistent_returns_none(self, db_connection):
        repo = UserPreferencesRepository(db_connection)
        result = await repo.get("user_1", "no_such_key")
        assert result is None

    async def test_upsert_overwrites_existing(self, db_connection):
        repo = UserPreferencesRepository(db_connection)
        await repo.set("user_1", "location", '{"city": "Portland"}')
        await repo.set("user_1", "location", '{"city": "Seattle"}')

        result = await repo.get("user_1", "location")
        assert result == '{"city": "Seattle"}'

    async def test_get_all_for_user(self, db_connection):
        repo = UserPreferencesRepository(db_connection)
        await repo.set("user_1", "location", '"Portland"')
        await repo.set("user_1", "wishlist", '["rug", "chair"]')

        all_prefs = await repo.get_all("user_1")
        assert all_prefs["location"] == '"Portland"'
        assert all_prefs["wishlist"] == '["rug", "chair"]'

    async def test_get_all_empty_user(self, db_connection):
        repo = UserPreferencesRepository(db_connection)
        all_prefs = await repo.get_all("user_nobody")
        assert all_prefs == {}

    async def test_different_users_isolated(self, db_connection):
        repo = UserPreferencesRepository(db_connection)
        await repo.set("user_a", "location", '"Portland"')
        await repo.set("user_b", "location", '"Seattle"')

        assert await repo.get("user_a", "location") == '"Portland"'
        assert await repo.get("user_b", "location") == '"Seattle"'

    async def test_delete_preference(self, db_connection):
        repo = UserPreferencesRepository(db_connection)
        await repo.set("user_1", "location", '"Portland"')
        deleted = await repo.delete("user_1", "location")
        assert deleted is True
        assert await repo.get("user_1", "location") is None

    async def test_delete_nonexistent_returns_false(self, db_connection):
        repo = UserPreferencesRepository(db_connection)
        deleted = await repo.delete("user_1", "no_such_key")
        assert deleted is False

    async def test_set_preserves_other_keys(self, db_connection):
        repo = UserPreferencesRepository(db_connection)
        await repo.set("user_1", "location", '"Portland"')
        await repo.set("user_1", "wishlist", '["rug"]')

        # Update one key, other should be untouched
        await repo.set("user_1", "location", '"Seattle"')
        assert await repo.get("user_1", "wishlist") == '["rug"]'
