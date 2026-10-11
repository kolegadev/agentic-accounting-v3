# /Users/johnpellew/agentic-accounting-v3/api/tests/test_coa_delete_guard.py

"""Contract F39 — the account delete guard.

Deleting an account with ANY transactions (Postgres posted scope OR a
Formance posting) must be refused with :class:`AccountInUseError` (409).
Unused accounts soft-delete exactly as before.  The Formance client and
the TransactionService are mocked at their module seams, following the
existing unit-test convention.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from src.index import app
from src.models.account import Account
from src.services.coa_service import (
    AccountInUseError,
    AccountNotFoundError,
    CoaService,
)
from tests.conftest import NOW


def make_account_obj(code: str = "1000", entity_id=None) -> Account:
    return Account(
        id=uuid.uuid4(),
        code=code,
        name="Bank Current Account",
        category="Asset",
        type="Bank",
        vat_rate=None,
        parent_id=None,
        entity_id=entity_id,
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def mock_db() -> AsyncMock:
    """Return an AsyncMock that behaves like an async SQLAlchemy session."""
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()

    async def _refresh(obj: object) -> None:
        if hasattr(obj, "id") and obj.id is None:
            obj.id = uuid.uuid4()  # type: ignore[attr-defined]
        if hasattr(obj, "is_active") and obj.is_active is None:
            obj.is_active = True  # type: ignore[attr-defined]
        if hasattr(obj, "created_at") and obj.created_at is None:
            obj.created_at = NOW  # type: ignore[attr-defined]
        if hasattr(obj, "updated_at") and obj.updated_at is None:
            obj.updated_at = NOW  # type: ignore[attr-defined]

    db.refresh = _refresh
    return db


def _account_fetch(mock_db: AsyncMock, account: Account) -> None:
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = account
    mock_db.execute.return_value = mock_result


def _patch_postgres(return_value: dict):
    return patch(
        "src.services.transaction_service.TransactionService.list_for_account",
        new_callable=AsyncMock,
        return_value=return_value,
    )


def _patch_formance(data: list, side_effect: Exception | None = None):
    return patch(
        "src.services.coa_service.formance_client.list_account_transactions",
        new_callable=AsyncMock,
        return_value={"cursor": {}, "data": data},
        side_effect=side_effect,
    )


# ---------------------------------------------------------------------------
# Service-level guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_soft_delete_used_by_postgres_raises_account_in_use(
    mock_db: AsyncMock,
) -> None:
    account = make_account_obj()
    _account_fetch(mock_db, account)

    with _patch_postgres(
        {"code": "1000", "items": [{"balance_pence": 100}], "count": 1}
    ):
        with pytest.raises(AccountInUseError) as exc_info:
            await CoaService.soft_delete_account(mock_db, account.id)

    assert exc_info.value.status_code == 409
    assert "cannot be deleted" in exc_info.value.message
    assert "1000" in exc_info.value.message
    assert account.is_active is True  # never flipped


@pytest.mark.asyncio
async def test_soft_delete_used_by_formance_only_raises_account_in_use(
    mock_db: AsyncMock,
) -> None:
    entity_id = uuid.uuid4()
    account = make_account_obj(entity_id=entity_id)
    _account_fetch(mock_db, account)

    with (
        _patch_postgres({"code": "1000", "items": [], "count": 0}),
        _patch_formance([{"txid": 1, "postings": [], "metadata": {}}]),
        patch(
            "src.services.coa_service.get_ledger_for_entity",
            new_callable=AsyncMock,
        ) as ledger,
    ):
        ledger.return_value = "acme-ledger"
        with pytest.raises(AccountInUseError) as exc_info:
            await CoaService.soft_delete_account(mock_db, account.id)

    assert exc_info.value.status_code == 409
    ledger.assert_awaited_once()
    assert ledger.await_args.args[1] == entity_id
    assert account.is_active is True


@pytest.mark.asyncio
async def test_soft_delete_unused_account_succeeds(mock_db: AsyncMock) -> None:
    account = make_account_obj()
    _account_fetch(mock_db, account)

    with (
        _patch_postgres({"code": "1000", "items": [], "count": 0}),
        _patch_formance([]),
    ):
        result = await CoaService.soft_delete_account(mock_db, account.id)

    assert result.is_active is False
    mock_db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_soft_delete_not_found_unchanged(mock_db: AsyncMock) -> None:
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_result

    with pytest.raises(AccountNotFoundError):
        await CoaService.soft_delete_account(mock_db, uuid.uuid4())


@pytest.mark.asyncio
async def test_account_in_use_legacy_scope_checks_postgres_only(
    mock_db: AsyncMock,
) -> None:
    """A NULL-scope account has no entity ledger — Formance is skipped."""
    with (
        _patch_postgres({"code": "1000", "items": [], "count": 0}),
        _patch_formance([]) as formance,
    ):
        result = await CoaService.account_in_use(
            mock_db, "1000", entity_id=None
        )

    assert result is False
    formance.assert_not_awaited()


@pytest.mark.asyncio
async def test_account_in_use_formance_unreachable_degrades_to_not_used(
    mock_db: AsyncMock,
) -> None:
    entity_id = uuid.uuid4()
    with (
        _patch_postgres({"code": "1000", "items": [], "count": 0}),
        _patch_formance(
            [], side_effect=httpx.ConnectError("connection refused")
        ) as formance,
        patch(
            "src.services.coa_service.get_ledger_for_entity",
            new_callable=AsyncMock,
        ) as ledger,
    ):
        ledger.return_value = "acme-ledger"
        result = await CoaService.account_in_use(
            mock_db, "1000", entity_id=entity_id
        )

    assert result is False
    formance.assert_awaited_once()


# ---------------------------------------------------------------------------
# Route-level mapping
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_delete_route_maps_account_in_use_to_409(client: TestClient) -> None:
    account_id = uuid.uuid4()

    with patch(
        "src.routes.coa.CoaService.soft_delete_account", new_callable=AsyncMock
    ) as soft_delete:
        soft_delete.side_effect = AccountInUseError("1000")
        response = client.delete(f"/api/v1/coa/{account_id}")

    assert response.status_code == 409
    body = response.json()
    assert "cannot be deleted" in body["detail"]
    assert "1000" in body["detail"]


def test_delete_route_404_unchanged(client: TestClient) -> None:
    account_id = uuid.uuid4()

    with patch(
        "src.routes.coa.CoaService.soft_delete_account", new_callable=AsyncMock
    ) as soft_delete:
        soft_delete.side_effect = AccountNotFoundError(str(account_id))
        response = client.delete(f"/api/v1/coa/{account_id}")

    assert response.status_code == 404


def test_delete_route_success_unchanged(client: TestClient) -> None:
    account_id = uuid.uuid4()

    from src.validators.account import AccountResponse

    deleted = AccountResponse(
        id=account_id,
        code="1000",
        name="Bank Current Account",
        category="Asset",
        type="Bank",
        vat_rate=None,
        is_active=False,
        created_at=NOW,
        updated_at=NOW,
    )

    with patch(
        "src.routes.coa.CoaService.soft_delete_account", new_callable=AsyncMock
    ) as soft_delete:
        soft_delete.return_value = deleted
        response = client.delete(f"/api/v1/coa/{account_id}")

    assert response.status_code == 200
    assert response.json()["is_active"] is False


# ---------------------------------------------------------------------------
# Formance client method (existence check for the guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_account_transactions_normalises_like_list_transactions() -> None:
    """The guard's existence check hits
    GET {base}/{ledger}/accounts/{address}/transactions and normalises the
    cursor envelope + minor-unit amounts exactly like list_transactions."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "cursor": {
            "hasMore": False,
            "data": [
                {
                    "txid": 42,
                    "reference": "JE-1",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "postings": [
                        {
                            "source": "world",
                            "destination": "1000",
                            "amount": 123456,
                            "asset": "GBP/2",
                        }
                    ],
                }
            ],
        }
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value.get.return_value = mock_response

    with patch(
        "src.services.formance_client.httpx.AsyncClient",
        return_value=mock_client,
    ):
        from src.services.formance_client import FormanceClient

        result = await FormanceClient(
            "http://formance:3068"
        ).list_account_transactions("main", "1000", page_size=1)

    call_args = mock_client.__aenter__.return_value.get.call_args
    assert call_args.args[0] == (
        "http://formance:3068/main/accounts/1000/transactions"
    )
    assert call_args.kwargs["params"] == {"pageSize": 1}
    assert len(result["data"]) == 1
    assert result["data"][0]["txid"] == 42
    assert result["data"][0]["postings"][0]["amount"] == "1234.56"
    assert result["data"][0]["postings"][0]["asset"] == "GBP/2"
    assert result["cursor"]["hasMore"] is False
