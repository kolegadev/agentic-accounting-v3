"""Unit tests for ChatService — state management, response formatting, personas."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.chat_service import ChatService
from src.services.formatting import format_pence, render_table
from src.services.llm_router import LLMRouter
from src.services.tool_executor import ToolExecutor


class _FakeKatra:
    """Stand-in for the Katra client with memory persistence disabled."""

    enabled = False


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Return a mocked aioredis client."""
    mock = AsyncMock()
    mock.get = AsyncMock(return_value=None)
    mock.set = AsyncMock()
    return mock


@pytest.fixture
def chat_service(mock_redis: AsyncMock) -> ChatService:
    """Return a ChatService with mocked Redis and disabled Katra."""
    svc = ChatService()
    svc._redis = mock_redis
    svc._katra = _FakeKatra()
    return svc


# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_conversation_state_new_session(chat_service: ChatService, mock_redis: AsyncMock) -> None:
    """New session should return default state."""
    mock_redis.get.return_value = None
    state = await chat_service.get_conversation_state("session-1")
    assert state["session_id"] == "session-1"
    assert state["persona"] == "professional"
    assert state["history"] == []
    assert state["context"] == {}


@pytest.mark.asyncio
async def test_get_conversation_state_existing(chat_service: ChatService, mock_redis: AsyncMock) -> None:
    """Existing session should restore saved state."""
    saved = {
        "session_id": "session-2",
        "persona": "friendly",
        "history": [{"role": "user", "content": "hello"}],
        "context": {"contact_id": "abc"},
    }
    mock_redis.get.return_value = json.dumps(saved)
    state = await chat_service.get_conversation_state("session-2")
    assert state["persona"] == "friendly"
    assert len(state["history"]) == 1


@pytest.mark.asyncio
async def test_save_conversation_state(chat_service: ChatService, mock_redis: AsyncMock) -> None:
    """Should persist state to Redis with TTL."""
    state = {"session_id": "s", "persona": "minimal", "history": [], "context": {}}
    await chat_service.save_conversation_state("s", state)
    mock_redis.set.assert_called_once()


# ---------------------------------------------------------------------------
# process_message pipeline
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_process_message_routes_and_formats(chat_service: ChatService, mock_redis: AsyncMock) -> None:
    """Full pipeline: route → execute tool → format → save."""
    mock_redis.get.return_value = json.dumps({
        "session_id": "s1",
        "persona": "professional",
        "history": [],
        "context": {},
    })

    route_results = [
        {"tool": "gl.record_expense", "params": {"description": "Office supplies", "amount": 5000}},
        {"response": "Recorded."},
    ]
    with (
        patch.object(LLMRouter, "route", AsyncMock(side_effect=route_results)),
        patch.object(
            ToolExecutor,
            "execute",
            AsyncMock(return_value={"success": True, "result": {"formatted": "Recorded **£50.00** expense."}}),
        ),
    ):
        result = await chat_service.process_message("s1", "Paid £50 for office supplies at Tesco")
    assert result["session_id"] == "s1"
    assert result["tool_call"]["skill_id"] == "gl.record_expense"
    assert result["tool_call"]["params"].get("amount") == 5000
    assert "text" in result["message"]
    # History should have 3 entries (user + tool result + assistant)
    assert len(result["history"]) == 3
    mock_redis.set.assert_called()


@pytest.mark.asyncio
async def test_process_message_with_context(chat_service: ChatService, mock_redis: AsyncMock) -> None:
    """Context from previous turns should be injected."""
    mock_redis.get.return_value = json.dumps({
        "session_id": "s2",
        "persona": "friendly",
        "history": [],
        "context": {"contact_id": "abc-123"},
    })

    route_mock = AsyncMock(side_effect=[
        {"tool": "invoice.create", "params": {"contact_id": "abc-123"}},
        {"response": "Invoice created."},
    ])
    with (
        patch.object(LLMRouter, "route", route_mock),
        patch.object(
            ToolExecutor,
            "execute",
            AsyncMock(return_value={"success": True, "result": {"formatted": "Invoice **created**."}}),
        ),
    ):
        result = await chat_service.process_message("s2", "Create invoice for this client")
    assert result["tool_call"]["skill_id"] == "invoice.create"
    # contact_id should be injected
    assert result["tool_call"]["params"].get("contact_id") == "abc-123"
    # The context from previous turns must be passed into routing
    context_arg = route_mock.call_args_list[0].args[2]
    assert context_arg.get("contact_id") == "abc-123"


# ---------------------------------------------------------------------------
# Formatting helpers (current API — src/services/formatting.py)
# ---------------------------------------------------------------------------
class TestFormattingHelpers:
    """Tests for the formatting helpers that replaced ChatService.format_response."""

    def test_format_pence_pounds(self) -> None:
        assert format_pence(5000) == "£50.00"
        assert format_pence(120000) == "£1,200.00"

    def test_format_pence_negative_and_none(self) -> None:
        assert format_pence(-1500) == "-£15.00"
        assert format_pence(None) == ""

    def test_render_table_markdown(self) -> None:
        rows = [
            {"code": "1000", "name": "Bank Current Account"},
            {"code": "4000", "name": "Sales"},
        ]
        out = render_table(rows, [("code", "Code"), ("name", "Name")])
        assert out["count"] == 2
        assert out["truncated"] is False
        assert "| Code | Name |" in out["formatted"]
        assert "| 1000 | Bank Current Account |" in out["formatted"]
        assert "| 4000 | Sales |" in out["formatted"]

    def test_render_table_empty(self) -> None:
        out = render_table([], [("code", "Code")])
        assert out["count"] == 0
        assert out["truncated"] is False
        assert "No results found." in out["formatted"]
