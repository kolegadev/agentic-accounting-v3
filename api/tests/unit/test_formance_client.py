"""Unit tests for Formance Ledger v2 HTTP client adapter.

Covers the REAL wire format (observed against ledger v2.3.21 — see
docs/live-e2e.md): Numscript bodies with unbounded overdraft, minor-unit
amounts, the data-list-inside-``cursor`` envelope, and the normalised
canonical shapes every caller (parity, reports, mirror) consumes.
"""

from __future__ import annotations

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.formance_client import FormanceClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    return FormanceClient(base_url="http://formance:3068")


@pytest.fixture
def sample_postings():
    return [
        {"source": "revenue", "destination": "cash", "amount": "10.00", "asset": "GBP/2"},
        {"source": "cash", "destination": "expense", "amount": "10.00", "asset": "GBP/2"},
    ]


def _post_call(mock_client):
    return mock_client.__aenter__.return_value.post.call_args


# ---------------------------------------------------------------------------
# amount conversion helpers
# ---------------------------------------------------------------------------


def test_major_to_minor_and_back():
    from src.services.formance_client import _major_to_minor, _minor_to_major

    assert _major_to_minor("10.00", "GBP/2") == 1000
    assert _major_to_minor("10000.00", "GBP/2") == 1000000
    assert _minor_to_major(1000000, "GBP/2") == "10000.00"
    assert _minor_to_major(-200, "GBP/2") == "-2.00"
    assert _minor_to_major(5, "GBP/2") == "0.05"


def test_major_to_minor_rejects_sub_minor_amount():
    from src.services.formance_client import _major_to_minor

    with pytest.raises(ValueError):
        _major_to_minor("1.005", "GBP/2")


def test_build_numscript_multiple_postings():
    from src.services.formance_client import _build_numscript

    script = _build_numscript(
        [
            {"source": "3000", "destination": "1000", "amount": "20000.00", "asset": "GBP/2"},
            {"source": "1000", "destination": "6500", "amount": "100.00", "asset": "GBP/2"},
        ]
    )
    assert "send [GBP/2 2000000] (" in script
    assert "source = @3000 allowing unbounded overdraft" in script
    assert "destination = @1000" in script
    assert "send [GBP/2 10000] (" in script
    assert "source = @1000 allowing unbounded overdraft" in script
    assert "destination = @6500" in script


# ---------------------------------------------------------------------------
# create_transaction — real wire format: Numscript + minor units
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_transaction_sends_numscript(client, sample_postings):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [{"txid": 42, "postings": [], "metadata": {}}]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.post.return_value = mock_response

    with patch("src.services.formance_client.httpx.AsyncClient", return_value=mock_client):
        result = await client.create_transaction(
            ledger="main",
            postings=sample_postings,
            reference="JE-2026-0001",
            timestamp="2026-06-27T12:00:00Z",
            metadata={"desc": "Test"},
        )

    assert result["txid"] == 42
    call_args = _post_call(mock_client)
    assert call_args[0][0] == "http://formance:3068/main/transactions"
    body = call_args[1]["json"]
    assert set(body) == {"script", "reference", "timestamp", "metadata"}
    assert body["reference"] == "JE-2026-0001"
    assert body["timestamp"] == "2026-06-27T12:00:00Z"
    assert body["metadata"] == {"desc": "Test"}
    script = body["script"]["plain"]
    assert "source = @revenue allowing unbounded overdraft" in script
    assert "destination = @cash" in script
    assert "[GBP/2 1000]" in script  # £10.00 -> 1000 minor units


@pytest.mark.asyncio
async def test_create_transaction_minimal(client, sample_postings):
    """Create transaction with only required fields (no ref, timestamp, metadata)."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"data": [{"txid": 1}]}
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.post.return_value = mock_response

    with patch("src.services.formance_client.httpx.AsyncClient", return_value=mock_client):
        result = await client.create_transaction(
            ledger="main",
            postings=sample_postings,
        )

    assert result["txid"] == 1
    body = _post_call(mock_client)[1]["json"]
    assert "reference" not in body
    assert "timestamp" not in body
    assert "metadata" not in body


@pytest.mark.asyncio
async def test_create_transaction_http_error(client, sample_postings):
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Server error",
        request=MagicMock(),
        response=MagicMock(status_code=500),
    )
    mock_client.__aenter__.return_value.post.return_value = mock_response

    with patch("src.services.formance_client.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(httpx.HTTPStatusError):
            await client.create_transaction(
                ledger="main",
                postings=sample_postings,
            )


# ---------------------------------------------------------------------------
# get_transaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_transaction_normalises(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": {
            "txid": 42,
            "reference": "JE-2026-0001",
            "timestamp": "2026-06-27T12:00:00Z",
            "metadata": {"transaction_id": "abc"},
            "postings": [
                {
                    "source": "3000",
                    "destination": "1000",
                    "amount": 1000000,
                    "asset": "GBP/2",
                }
            ],
        }
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.get.return_value = mock_response

    with patch("src.services.formance_client.httpx.AsyncClient", return_value=mock_client):
        result = await client.get_transaction("main", 42)

    assert result["txid"] == 42
    assert result["postings"][0]["amount"] == "10000.00"
    mock_client.__aenter__.return_value.get.assert_called_once_with(
        "http://formance:3068/main/transactions/42"
    )


@pytest.mark.asyncio
async def test_get_transaction_not_found(client):
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Not found",
        request=MagicMock(),
        response=MagicMock(status_code=404),
    )
    mock_client.__aenter__.return_value.get.return_value = mock_response

    with patch("src.services.formance_client.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_transaction("main", 99999)


# ---------------------------------------------------------------------------
# list_transactions — real envelope: data inside cursor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_transactions_normalises_envelope(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "cursor": {
            "pageSize": 10,
            "hasMore": False,
            "data": [
                {
                    "txid": 1,
                    "reference": "A",
                    "timestamp": "2026-06-27T12:00:00Z",
                    "metadata": {"transaction_id": "t1"},
                    "postings": [
                        {
                            "source": "world",
                            "destination": "1000",
                            "amount": 1000000,
                            "asset": "GBP/2",
                        }
                    ],
                },
                {"txid": 2, "reference": "B", "metadata": {}, "postings": []},
            ]
        }
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.get.return_value = mock_response

    with patch("src.services.formance_client.httpx.AsyncClient", return_value=mock_client):
        result = await client.list_transactions("main", page_size=10)

    assert isinstance(result["cursor"], dict)
    assert result["cursor"]["hasMore"] is False
    assert result["cursor"]["pageSize"] == 10
    data = result["data"]
    assert [tx["txid"] for tx in data] == [1, 2]
    assert data[0]["postings"][0]["amount"] == "10000.00"
    assert data[0]["metadata"] == {"transaction_id": "t1"}
    assert data[1]["metadata"] == {}
    mock_client.__aenter__.return_value.get.assert_called_once_with(
        "http://formance:3068/main/transactions",
        params={"pageSize": 10},
    )


@pytest.mark.asyncio
async def test_list_transactions_with_cursor(client):
    """A follow-up page sends the opaque cursor ALONE (v2.3.x requirement)."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "cursor": {"pageSize": 50, "hasMore": True, "next": "cursor456", "data": []}
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.get.return_value = mock_response

    with patch("src.services.formance_client.httpx.AsyncClient", return_value=mock_client):
        result = await client.list_transactions("main", page_size=50, after="cursor123")

    assert result["cursor"]["hasMore"] is True
    assert result["cursor"]["next"] == "cursor456"
    mock_client.__aenter__.return_value.get.assert_called_once_with(
        "http://formance:3068/main/transactions",
        params={"cursor": "cursor123"},
    )


# ---------------------------------------------------------------------------
# get_balances — real envelope + per-address maps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_balances_normalises(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "cursor": {
            "pageSize": 15,
            "hasMore": False,
            "data": [
                {"1000": {"GBP/2": 1000000}},
                {"3000": {"GBP/2": -1000000}},
            ],
        }
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.get.return_value = mock_response

    with patch("src.services.formance_client.httpx.AsyncClient", return_value=mock_client):
        result = await client.get_balances("main")

    assert result == {
        "data": [
            {"address": "1000", "balances": {"GBP/2": "10000.00"}},
            {"address": "3000", "balances": {"GBP/2": "-10000.00"}},
        ]
    }
    mock_client.__aenter__.return_value.get.assert_called_once_with(
        "http://formance:3068/main/balances",
        params={"pageSize": 1000},
    )


@pytest.mark.asyncio
async def test_get_balances_filtered(client):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "cursor": {"pageSize": 15, "hasMore": False, "data": []}
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.get.return_value = mock_response

    with patch("src.services.formance_client.httpx.AsyncClient", return_value=mock_client):
        await client.get_balances("main", address="users:001")

    mock_client.__aenter__.return_value.get.assert_called_once_with(
        "http://formance:3068/main/balances",
        params={"pageSize": 1000, "address": "users:001"},
    )


# ---------------------------------------------------------------------------
# create_ledger — 204 new, 400 LEDGER_ALREADY_EXISTS duplicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_ledger_created(client):
    mock_response = MagicMock()
    mock_response.status_code = 204
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.post.return_value = mock_response

    with patch("src.services.formance_client.httpx.AsyncClient", return_value=mock_client):
        assert await client.create_ledger("entity-abc123") is True

    mock_client.__aenter__.return_value.post.assert_called_once_with(
        "http://formance:3068/v2/entity-abc123"
    )


@pytest.mark.asyncio
async def test_create_ledger_duplicate_400_tolerated(client):
    """v2.3.x answers 400 LEDGER_ALREADY_EXISTS on repeat — idempotent."""
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.json.return_value = {
        "errorCode": "LEDGER_ALREADY_EXISTS",
        "errorMessage": "ledger already exists",
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.post.return_value = mock_response

    with patch("src.services.formance_client.httpx.AsyncClient", return_value=mock_client):
        assert await client.create_ledger("entity-abc123") is True


@pytest.mark.asyncio
async def test_create_ledger_unreachable_returns_false(client):
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.post.side_effect = httpx.ConnectError(
        "Connection refused"
    )

    with patch("src.services.formance_client.httpx.AsyncClient", return_value=mock_client):
        assert await client.create_ledger("entity-abc123") is False


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_healthy(client):
    mock_response = MagicMock()
    mock_response.status_code = 200

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.get.return_value = mock_response

    with patch("src.services.formance_client.httpx.AsyncClient", return_value=mock_client):
        result = await client.health_check()

    assert result is True
    mock_client.__aenter__.return_value.get.assert_called_once_with(
        "http://formance:3068/_info"
    )


@pytest.mark.asyncio
async def test_health_check_unhealthy(client):
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.get.side_effect = Exception("Connection refused")

    with patch("src.services.formance_client.httpx.AsyncClient", return_value=mock_client):
        result = await client.health_check()

    assert result is False


@pytest.mark.asyncio
async def test_health_check_500_error(client):
    mock_response = MagicMock()
    mock_response.status_code = 500

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.get.return_value = mock_response

    with patch("src.services.formance_client.httpx.AsyncClient", return_value=mock_client):
        result = await client.health_check()

    assert result is False


# ---------------------------------------------------------------------------
# default base_url
# ---------------------------------------------------------------------------


def test_default_base_url():
    """Client without explicit base_url should use FORNANCE_URL env var."""
    client = FormanceClient()
    assert client.base_url == "http://localhost:3068"


def test_custom_base_url():
    client = FormanceClient(base_url="http://custom:9999")
    assert client.base_url == "http://custom:9999"


def test_base_url_trailing_slash():
    client = FormanceClient(base_url="http://formance:3068/")
    assert client.base_url == "http://formance:3068"
