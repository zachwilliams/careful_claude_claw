"""Shared test fixtures for CarefulClaudeClaw."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import careful_claude_claw.db as db_module


class AsyncIterator:
    """Helper for mocking async iterators."""

    def __init__(self, items):
        self.items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.items)
        except StopIteration:
            raise StopAsyncIteration


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point tests at a fresh temporary database."""
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "test.db")
    db_module.init_db()


@pytest.fixture
def mock_sdk_client():
    """Create a mock ClaudeSDKClient."""
    with patch("careful_claude_claw.persistent_orchestrator.ClaudeSDKClient") as mock_cls:
        client = MagicMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.query = AsyncMock()
        client.receive_messages = AsyncMock(return_value=AsyncIterator([]))
        mock_cls.return_value = client
        yield client
