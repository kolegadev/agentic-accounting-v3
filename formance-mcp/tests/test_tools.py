"""Unit tests for the Formance MCP tools with mocked httpx."""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

import httpx
import pytest

import formance_mcp.server as server_mod
from formance_mcp.client import FormanceClient
from formance_mcp.server import FormanceToolError

POSTING = {
    "source": "world",
    "destination": "alice",
    "amount": "12.34",
    "asset": "USD/2",
}


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> FormanceClient:
    return FormanceClient(
        base_url="http://formance.test", transport=httpx.MockTransport(handler)
    )


@pytest.fixture
def mock(monkeypatch):
    """Install a MockTransport-backed client as the process-wide client."""

    def _install(handler: Callable[[httpx.Request], httpx.Response]) -> FormanceClient:
        client = make_client(handler)
        monkeypatch.setattr(server_mod, "get_client", lambda: client)
        return client

    return _install


def json_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


async def test_health(mock):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/_info"
        return httpx.Response(200, json={"version": "v2.3.0", "service": "ledger"})

    mock(handler)
    result = await server_mod.health()
    assert result == {
        "status": "ok",
        "version": "v2.3.0",
        "info": {"version": "v2.3.0", "service": "ledger"},
    }


# ---------------------------------------------------------------------------
# create_transaction
# ---------------------------------------------------------------------------


async def test_create_transaction_happy_path(mock):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json_body(request)
        captured["headers"] = request.headers
        return httpx.Response(
            200, json={"data": {"txid": 1, "postings": [POSTING]}}
        )

    mock(handler)
    result = await server_mod.create_transaction(
        "main",
        [POSTING],
        reference="JE-1",
        timestamp="2026-10-10T10:00:00Z",
        metadata={"note": "hello"},
        idempotency_key="idem-1",
    )
    assert result == {"data": {"txid": 1, "postings": [POSTING]}}
    assert captured["method"] == "POST"
    assert captured["path"] == "/main/transactions"
    assert captured["body"]["postings"] == [POSTING]
    assert captured["body"]["reference"] == "JE-1"
    assert captured["body"]["timestamp"] == "2026-10-10T10:00:00Z"
    assert captured["body"]["metadata"] == {"note": "hello"}
    assert captured["headers"].get("Idempotency-Key") == "idem-1"


async def test_create_transaction_omits_optional_fields(mock):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json_body(request)
        captured["headers"] = request.headers
        return httpx.Response(200, json={"data": {"txid": 2}})

    mock(handler)
    await server_mod.create_transaction("main", [POSTING])
    assert captured["body"] == {"postings": [POSTING]}
    assert captured["headers"].get("Idempotency-Key") is None


@pytest.mark.parametrize("amount", ["0", "1", "1.5", "0.01", "1234.56", "100"])
async def test_create_transaction_accepts_valid_amounts(mock, amount):
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json_body(request))
        return httpx.Response(200, json={"data": {"txid": 9}})

    mock(handler)
    result = await server_mod.create_transaction(
        "main", [dict(POSTING, amount=amount)]
    )
    assert sent[0]["postings"][0]["amount"] == amount
    assert result == {"data": {"txid": 9}}


@pytest.mark.parametrize(
    "bad", ["1.555", "abc", "", "1.2.3", "-5.00", "1e3", "5,00", "12.", ".5", "0.001"]
)
async def test_create_transaction_rejects_invalid_amount_strings(mock, bad):
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(500, json={})

    mock(handler)
    with pytest.raises(FormanceToolError, match="invalid amount"):
        await server_mod.create_transaction("main", [dict(POSTING, amount=bad)])
    assert sent == []


@pytest.mark.parametrize("bad", [1.5, 5, None, {"nested": "1.00"}, ["1.00"]])
async def test_create_transaction_rejects_non_string_amounts(mock, bad):
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(500, json={})

    mock(handler)
    with pytest.raises(FormanceToolError, match="decimal string"):
        await server_mod.create_transaction("main", [dict(POSTING, amount=bad)])
    assert sent == []


async def test_create_transaction_rejects_missing_posting_field(mock):
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(500, json={})

    mock(handler)
    bad = {"source": "world", "destination": "alice", "amount": "1.00"}
    with pytest.raises(FormanceToolError, match="missing required field 'asset'"):
        await server_mod.create_transaction("main", [bad])
    assert sent == []


async def test_create_transaction_rejects_empty_postings(mock):
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(500, json={})

    mock(handler)
    with pytest.raises(FormanceToolError, match="non-empty"):
        await server_mod.create_transaction("main", [])
    assert sent == []


# ---------------------------------------------------------------------------
# get_transaction
# ---------------------------------------------------------------------------


async def test_get_transaction_happy(mock):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"data": {"txid": 42, "postings": [POSTING]}})

    mock(handler)
    result = await server_mod.get_transaction("main", 42)
    assert captured["path"] == "/main/transactions/42"
    assert result == {"data": {"txid": 42, "postings": [POSTING]}}


# ---------------------------------------------------------------------------
# Error mapping: Formance 4xx/5xx/connection errors become structured errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,payload",
    [
        (400, {"errorCode": "INTERPRETER_PARSE", "errorMessage": "unexpected token"}),
        (422, {"errorCode": "VALIDATION", "errorMessage": "bad posting"}),
        (500, {"error": "internal"}),
    ],
)
async def test_formance_http_error_maps_to_structured_tool_error(mock, status, payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    mock(handler)
    with pytest.raises(FormanceToolError) as excinfo:
        await server_mod.get_transaction("main", 42)
    err = excinfo.value
    assert err.status == status
    assert err.body == payload
    assert str(status) in str(err)
    assert str(payload) in str(err)


async def test_connection_error_maps_to_structured_tool_error(mock):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    mock(handler)
    with pytest.raises(FormanceToolError) as excinfo:
        await server_mod.get_transaction("main", 42)
    err = excinfo.value
    assert err.status is None
    assert "connection failure" in str(err)
    assert "ConnectError" in str(err)


# ---------------------------------------------------------------------------
# list_transactions (cursor pagination)
# ---------------------------------------------------------------------------


async def test_list_transactions_follows_cursor(mock):
    calls: list[Optional[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("after"))
        assert request.url.params["pageSize"] == "100"
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "cursor": {"hasMore": True, "next": "10"},
                    "data": [{"txid": 10, "postings": [POSTING]}],
                },
            )
        return httpx.Response(
            200,
            json={
                "cursor": {"hasMore": False, "next": None},
                "data": [{"txid": 20, "postings": [POSTING]}],
            },
        )

    mock(handler)
    result = await server_mod.list_transactions("main")
    assert calls == [None, "10"]
    assert [tx["txid"] for tx in result["data"]] == [10, 20]
    assert result["cursor"] == {"hasMore": False, "next": None}


async def test_list_transactions_passes_page_size_and_after(mock):
    calls: list[Optional[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("after"))
        assert request.url.params["pageSize"] == "25"
        return httpx.Response(
            200,
            json={
                "cursor": {"hasMore": False, "next": None},
                "data": [{"txid": 5}],
            },
        )

    mock(handler)
    result = await server_mod.list_transactions("main", page_size=25, after="5")
    assert calls == ["5"]
    assert [tx["txid"] for tx in result["data"]] == [5]


async def test_list_transactions_stops_when_cursor_does_not_advance(mock):
    calls: list[Optional[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("after"))
        return httpx.Response(
            200,
            json={"cursor": {"hasMore": True, "next": "10"}, "data": [{"txid": 1}]},
        )

    mock(handler)
    result = await server_mod.list_transactions("main")
    assert calls == [None, "10"]
    assert len(result["data"]) == 2


# ---------------------------------------------------------------------------
# get_balances
# ---------------------------------------------------------------------------


async def test_get_balances_preserves_structure(mock):
    captured: list[Optional[str]] = []
    payload = {
        "data": {"alice": {"USD/2": "12.34"}, "bob": {"EUR/2": "5.00", "USD/2": "7.00"}}
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.params.get("address"))
        return httpx.Response(200, json=payload)

    mock(handler)
    result = await server_mod.get_balances("main")
    assert captured == [None]
    assert result == payload


async def test_get_balances_filters_by_address(mock):
    captured: list[Optional[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.params.get("address"))
        return httpx.Response(200, json={"data": {"alice": {"USD/2": "12.34"}}})

    mock(handler)
    result = await server_mod.get_balances("main", address="alice")
    assert captured == ["alice"]
    assert result == {"data": {"alice": {"USD/2": "12.34"}}}


# ---------------------------------------------------------------------------
# list_ledgers
# ---------------------------------------------------------------------------


async def test_list_ledgers_parses_json_root(mock):
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/_info":
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json={"ledgers": [{"name": "main"}, {"name": "eur"}]})

    mock(handler)
    result = await server_mod.list_ledgers()
    assert paths == ["/_info", "/"]
    assert result["ledgers"] == ["main", "eur"]
    assert result["info"] is None


async def test_list_ledgers_tolerates_non_json_root(mock):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/_info":
            return httpx.Response(200, json={"version": "v2.3.0", "service": "ledger"})
        return httpx.Response(
            200, text="<html>not json</html>", headers={"content-type": "text/html"}
        )

    mock(handler)
    result = await server_mod.list_ledgers()
    assert result == {
        "info": {"version": "v2.3.0", "service": "ledger"},
        "ledgers": [],
    }


async def test_list_ledgers_no_source_raises_structured_error(mock):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/_info":
            return httpx.Response(404, json={"error": "nf"})
        return httpx.Response(404, text="not found")

    mock(handler)
    with pytest.raises(FormanceToolError, match="no ledger listing"):
        await server_mod.list_ledgers()


# ---------------------------------------------------------------------------
# run_script
# ---------------------------------------------------------------------------


async def test_run_script_posts_plain(mock):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/main/script"
        captured["body"] = json_body(request)
        return httpx.Response(200, json={"tx": {"id": 7}})

    mock(handler)
    script = "send [USD/2 100] (source=@world destination=@alice)"
    result = await server_mod.run_script("main", script)
    assert captured["body"] == {"plain": script}
    assert result == {"tx": {"id": 7}}


# ---------------------------------------------------------------------------
# env config
# ---------------------------------------------------------------------------


def test_client_default_env_config(monkeypatch):
    monkeypatch.delenv("FORNANCE_LEDGER_URL", raising=False)
    monkeypatch.delenv("FORMANCE_AUTH_TOKEN", raising=False)
    client = FormanceClient()
    assert client.base_url == "http://localhost:3068"
    assert client.auth_token is None


def test_client_env_config(monkeypatch):
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://ledger:3068/")
    monkeypatch.setenv("FORMANCE_AUTH_TOKEN", "secret-token")
    client = FormanceClient()
    assert client.base_url == "http://ledger:3068"
    assert client.auth_token == "secret-token"


async def test_auth_token_sent_as_bearer(monkeypatch):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"version": "v2.3.0", "service": "ledger"})

    client = FormanceClient(
        base_url="http://x", auth_token="tok-123", transport=httpx.MockTransport(handler)
    )
    monkeypatch.setattr(server_mod, "get_client", lambda: client)
    await server_mod.health()
    assert captured["auth"] == "Bearer tok-123"


def test_get_transport_default(monkeypatch):
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    assert server_mod.get_transport() == "stdio"


def test_get_transport_env(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    assert server_mod.get_transport() == "streamable-http"


def test_main_rejects_unknown_transport(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "carrier-pigeon")
    with pytest.raises(SystemExit, match="unsupported MCP_TRANSPORT"):
        server_mod.main()
