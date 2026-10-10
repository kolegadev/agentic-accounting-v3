"""Unit tests for the Formance MCP tools with mocked httpx.

Pins the REAL wire contract (observed against ``ghcr.io/formancehq/ledger``
v2.3.21 — see docs/live-e2e.md): Numscript bodies with minor-unit integer
amounts and unbounded overdraft, the data-list-inside-``cursor`` envelope,
minor->major normalisation, cursor-only follow-ups and the 204/400
create-ledger tolerance.
"""

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

# The exact Numscript the client must emit for POSTING (12.34 -> 1234 minor).
POSTING_SCRIPT = (
    "send [USD/2 1234] (\n"
    "\tsource = @world allowing unbounded overdraft\n"
    "\tdestination = @alice\n"
    ")"
)


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
# create_transaction — real wire format: Numscript + minor-unit ints
# ---------------------------------------------------------------------------


async def test_create_transaction_happy_path(mock):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json_body(request)
        captured["headers"] = request.headers
        return httpx.Response(
            200, json={"data": [{"txid": 1, "postings": [POSTING]}]}
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
    # The real envelope {"data": [{txid, ...}]} collapses to {"txid": N}.
    assert result == {"txid": 1}
    assert captured["method"] == "POST"
    assert captured["path"] == "/main/transactions"
    assert captured["body"]["script"] == {"plain": POSTING_SCRIPT}
    assert "postings" not in captured["body"]
    assert captured["body"]["reference"] == "JE-1"
    assert captured["body"]["timestamp"] == "2026-10-10T10:00:00Z"
    assert captured["body"]["metadata"] == {"note": "hello"}
    assert captured["headers"].get("Idempotency-Key") == "idem-1"


async def test_create_transaction_omits_optional_fields(mock):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json_body(request)
        captured["headers"] = request.headers
        return httpx.Response(200, json={"data": [{"txid": 2}]})

    mock(handler)
    result = await server_mod.create_transaction("main", [POSTING])
    assert result == {"txid": 2}
    assert captured["body"] == {"script": {"plain": POSTING_SCRIPT}}
    assert captured["headers"].get("Idempotency-Key") is None


async def test_create_transaction_numscript_exact_shape(mock):
    """The Numscript body is byte-identical to the F14 API client's."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json_body(request)
        return httpx.Response(200, json={"data": [{"txid": 9}]})

    mock(handler)
    await server_mod.create_transaction(
        "main",
        [
            {"source": "3000", "destination": "1000", "amount": "20000.00", "asset": "GBP/2"},
            {"source": "1000", "destination": "6500", "amount": "100.00", "asset": "GBP/2"},
        ],
    )
    script = captured["body"]["script"]["plain"]
    assert script == (
        "send [GBP/2 2000000] (\n"
        "\tsource = @3000 allowing unbounded overdraft\n"
        "\tdestination = @1000\n"
        ")\n"
        "send [GBP/2 10000] (\n"
        "\tsource = @1000 allowing unbounded overdraft\n"
        "\tdestination = @6500\n"
        ")"
    )


@pytest.mark.parametrize(
    "amount,minor",
    [
        ("0", 0),
        ("1", 100),
        ("1.5", 150),
        ("0.01", 1),
        ("1234.56", 123456),
        ("100", 10000),
    ],
)
async def test_create_transaction_converts_amounts_to_minor_units_exactly(
    mock, amount, minor
):
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json_body(request))
        return httpx.Response(200, json={"data": [{"txid": 9}]})

    mock(handler)
    result = await server_mod.create_transaction(
        "main", [dict(POSTING, amount=amount)]
    )
    assert result == {"txid": 9}
    assert sent[0]["script"]["plain"] == (
        f"send [USD/2 {minor}] (\n"
        "\tsource = @world allowing unbounded overdraft\n"
        "\tdestination = @alice\n"
        ")"
    )


async def test_create_transaction_respects_asset_precision(mock):
    """GBP/4 -> 4 minor units per major unit (12.34 -> 123400)."""
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json_body(request))
        return httpx.Response(200, json={"data": [{"txid": 9}]})

    mock(handler)
    await server_mod.create_transaction(
        "main", [dict(POSTING, amount="12.34", asset="GBP/4")]
    )
    assert "send [GBP/4 123400] (" in sent[0]["script"]["plain"]


async def test_create_transaction_unrepresentable_amount_is_structured_error(mock):
    """1.55 in a GBP/1 asset (1 minor unit per major unit) cannot be
    represented exactly — a structured tool error, never a float rounding."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(500, json={})

    mock(handler)
    with pytest.raises(FormanceToolError, match="not representable"):
        await server_mod.create_transaction(
            "main", [dict(POSTING, amount="1.55", asset="GBP/1")]
        )
    assert sent == []


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
# get_transaction — real envelope + minor->major normalisation
# ---------------------------------------------------------------------------


async def test_get_transaction_happy(mock):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "data": {
                    "txid": 42,
                    "reference": "JE-1",
                    "timestamp": "2026-10-10T10:00:00Z",
                    "metadata": {"transaction_id": "t1"},
                    "postings": [
                        {
                            "source": "world",
                            "destination": "alice",
                            "amount": 1234,
                            "asset": "USD/2",
                        }
                    ],
                }
            },
        )

    mock(handler)
    result = await server_mod.get_transaction("main", 42)
    assert captured["path"] == "/main/transactions/42"
    assert result["data"]["txid"] == 42
    assert result["data"]["reference"] == "JE-1"
    assert result["data"]["metadata"] == {"transaction_id": "t1"}
    assert result["data"]["postings"] == [
        {
            "source": "world",
            "destination": "alice",
            "amount": "12.34",
            "asset": "USD/2",
        }
    ]


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
# list_transactions — real envelope: data inside cursor, cursor-only follow-ups
# ---------------------------------------------------------------------------


def _tx(txid: int, minor: int = 1234) -> dict[str, Any]:
    return {
        "txid": txid,
        "reference": f"R{txid}",
        "timestamp": "2026-10-10T10:00:00Z",
        "metadata": {},
        "postings": [
            {
                "source": "world",
                "destination": "alice",
                "amount": minor,
                "asset": "USD/2",
            }
        ],
    }


async def test_list_transactions_follows_cursor(mock):
    calls: list[Optional[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("cursor"))
        if len(calls) == 1:
            assert request.url.params.get("pageSize") == "100"
            assert "after" not in request.url.params
            return httpx.Response(
                200,
                json={"cursor": {"pageSize": 100, "hasMore": True, "next": "10",
                                 "data": [_tx(10)]}},
            )
        assert request.url.params.get("pageSize") is None
        assert "after" not in request.url.params
        return httpx.Response(
            200,
            json={"cursor": {"pageSize": 100, "hasMore": False, "next": None,
                             "data": [_tx(20)]}},
        )

    mock(handler)
    result = await server_mod.list_transactions("main")
    assert calls == [None, "10"]
    assert [tx["txid"] for tx in result["data"]] == [10, 20]
    assert result["data"][0]["postings"][0]["amount"] == "12.34"
    assert result["cursor"] == {"hasMore": False, "next": None}


async def test_list_transactions_passes_after_as_cursor_alone(mock):
    calls: list[Optional[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("cursor"))
        assert "pageSize" not in request.url.params
        assert "after" not in request.url.params
        return httpx.Response(
            200,
            json={"cursor": {"pageSize": 25, "hasMore": False, "next": None,
                             "data": [_tx(5)]}},
        )

    mock(handler)
    result = await server_mod.list_transactions("main", page_size=25, after="5")
    assert calls == ["5"]
    assert [tx["txid"] for tx in result["data"]] == [5]


async def test_list_transactions_stops_when_cursor_does_not_advance(mock):
    calls: list[Optional[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("cursor"))
        return httpx.Response(
            200,
            json={"cursor": {"pageSize": 100, "hasMore": True, "next": "10",
                             "data": [_tx(1)]}},
        )

    mock(handler)
    result = await server_mod.list_transactions("main")
    assert calls == [None, "10"]
    assert len(result["data"]) == 2


async def test_list_transactions_normalises_top_level_data_fallback(mock):
    """Without a cursor dict, a top-level data list is still normalised."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [_tx(7, minor=1000000)]},
        )

    mock(handler)
    result = await server_mod.list_transactions("main")
    assert [tx["txid"] for tx in result["data"]] == [7]
    assert result["data"][0]["postings"][0]["amount"] == "10000.00"
    assert result["cursor"]["hasMore"] is False


# ---------------------------------------------------------------------------
# get_balances — real envelope + per-address/per-asset normalisation
# ---------------------------------------------------------------------------


async def test_get_balances_normalises_envelope(mock):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "cursor": {
                    "pageSize": 1000,
                    "hasMore": False,
                    "data": [
                        {"alice": {"USD/2": 1234}},
                        {"bob": {"EUR/2": 500, "USD/2": 700}},
                    ],
                }
            },
        )

    mock(handler)
    result = await server_mod.get_balances("main")
    assert captured["params"] == {"pageSize": "1000"}
    assert result == {
        "data": [
            {"address": "alice", "balances": {"USD/2": "12.34"}},
            {"address": "bob", "balances": {"EUR/2": "5.00", "USD/2": "7.00"}},
        ]
    }


async def test_get_balances_converts_negative_minor_units(mock):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "cursor": {
                    "pageSize": 1000,
                    "hasMore": False,
                    "data": [{"4000": {"GBP/2": -10000}}],
                }
            },
        )

    mock(handler)
    result = await server_mod.get_balances("main")
    assert result == {"data": [{"address": "4000", "balances": {"GBP/2": "-100.00"}}]}


async def test_get_balances_filters_by_address(mock):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={"cursor": {"pageSize": 1000, "hasMore": False,
                             "data": [{"alice": {"USD/2": 1234}}]}},
        )

    mock(handler)
    result = await server_mod.get_balances("main", address="alice")
    assert captured["params"] == {"pageSize": "1000", "address": "alice"}
    assert result == {"data": [{"address": "alice", "balances": {"USD/2": "12.34"}}]}


# ---------------------------------------------------------------------------
# create_ledger — 204 new, 400 LEDGER_ALREADY_EXISTS duplicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,body",
    [
        (204, None),
        (200, {"data": {"name": "main"}}),
        (201, {}),
        (409, {"errorCode": "LEDGER_ALREADY_EXISTS"}),
    ],
)
async def test_create_ledger_idempotent_success(mock, status, body):
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        captured["path"] = request.url.path
        return httpx.Response(status, json=body)

    mock(handler)
    result = await server_mod.create_ledger("main")
    assert captured["path"] == "/v2/main"
    assert result == {"ledger": "main", "created": True}


async def test_create_ledger_tolerates_400_ledger_already_exists(mock):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"errorCode": "LEDGER_ALREADY_EXISTS",
                  "errorMessage": "ledger already exists"},
        )

    mock(handler)
    result = await server_mod.create_ledger("main")
    assert result == {"ledger": "main", "created": True}


async def test_create_ledger_other_4xx_raises_structured_error(mock):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"errorCode": "VALIDATION", "errorMessage": "bad name"}
        )

    mock(handler)
    with pytest.raises(FormanceToolError) as excinfo:
        await server_mod.create_ledger("main")
    err = excinfo.value
    assert err.status == 400
    assert "VALIDATION" in str(err)


async def test_create_ledger_connection_error_raises_structured_error(mock):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    mock(handler)
    with pytest.raises(FormanceToolError) as excinfo:
        await server_mod.create_ledger("main")
    assert excinfo.value.status is None


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
