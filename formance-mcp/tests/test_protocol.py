"""Real in-memory MCP protocol round-trip against the mcp SDK.

Proves the negotiated protocol is the SDK's current version (not 0.1.0) and
that initialize -> tools/list -> tools/call works end to end over the
SDK's own in-memory transport, with structured errors on the wire.
"""

from __future__ import annotations

import json

import httpx
import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

import formance_mcp.server as server_mod
from formance_mcp.client import FormanceClient

ALL_TOOLS = {
    "health",
    "create_transaction",
    "get_transaction",
    "list_transactions",
    "get_balances",
    "list_ledgers",
    "run_script",
}


def _client_with(handler) -> FormanceClient:
    return FormanceClient(
        base_url="http://formance.test", transport=httpx.MockTransport(handler)
    )


@pytest.fixture
def healthy_formance(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/_info"
        return httpx.Response(200, json={"version": "v2.3.0", "service": "ledger"})

    monkeypatch.setattr(server_mod, "get_client", lambda: _client_with(handler))


async def test_initialize_lists_all_tools_and_calls_health(healthy_formance):
    server = server_mod.create_server()

    async with InMemoryTransport(server) as streams:
        client_read, client_write = streams
        async with ClientSession(client_read, client_write) as session:
            init = await session.initialize()
            assert init.protocol_version
            assert init.protocol_version != "0.1.0"

            listed = await session.list_tools()
            by_name = {tool.name: tool for tool in listed.tools}
            assert set(by_name) == ALL_TOOLS

            for name, tool in by_name.items():
                assert tool.input_schema is not None, f"{name} missing input schema"
                assert isinstance(tool.input_schema, dict), f"{name} schema not a dict"

            create_schema = by_name["create_transaction"].input_schema
            props = set(create_schema.get("properties", {}))
            assert {
                "ledger",
                "postings",
                "reference",
                "timestamp",
                "metadata",
                "idempotency_key",
            } <= props
            assert {"ledger", "postings"} <= set(create_schema.get("required", []))

            result = await session.call_tool("health", {})
            assert not result.is_error
            assert result.content
            payload = json.loads(result.content[0].text)
            assert payload["status"] == "ok"
            assert payload["version"] == "v2.3.0"


async def test_tool_error_is_structured_on_the_wire(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"errorCode": "NOT_FOUND", "errorMessage": "no such tx"}
        )

    monkeypatch.setattr(server_mod, "get_client", lambda: _client_with(handler))
    server = server_mod.create_server()

    async with InMemoryTransport(server) as streams:
        client_read, client_write = streams
        async with ClientSession(client_read, client_write) as session:
            await session.initialize()
            result = await session.call_tool(
                "get_transaction", {"ledger": "main", "txid": 42}
            )
            assert result.is_error is True
            text = result.content[0].text
            assert "400" in text
            assert "NOT_FOUND" in text
