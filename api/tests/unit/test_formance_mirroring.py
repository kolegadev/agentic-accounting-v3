"""Unit tests for correct double-entry Formance mirroring (Contract F1).

Verifies that posted and reversed journal entries are mirrored to the
Formance ledger as paired (source, destination) postings with no ``world``
contra account, exact decimal amounts, and preserved reference/metadata.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError

from src.models.account import Account
from src.models.transaction import Posting, Transaction
from src.services.transaction_service import (
    TransactionService,
    build_formance_postings,
)
from src.validators.transaction import PostingCreate

NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_account(code: str) -> Account:
    return Account(
        id=uuid.uuid4(),
        code=code,
        name=code,
        category="Asset",
        type="Bank",
        is_active=True,
    )


def posting_create(account: Account, *, debit: int = 0, credit: int = 0) -> PostingCreate:
    return PostingCreate(account_id=account.id, debit_amount=debit, credit_amount=credit)


def codes_for(*accounts: Account) -> dict[uuid.UUID, str]:
    return {a.id: a.code for a in accounts}


def orm_posting(account: Account, *, debit: int = 0, credit: int = 0) -> Posting:
    return Posting(
        id=uuid.uuid4(),
        transaction_id=uuid.uuid4(),
        account_id=account.id,
        debit_amount=debit,
        credit_amount=credit,
    )


def make_transaction(accounts, postings: list[Posting]) -> Transaction:
    tx_id = uuid.uuid4()
    tx = Transaction(
        id=tx_id,
        reference="JE-2026-0042",
        description="Sales invoice",
        currency="GBP",
        status="posted",
        effective_date=None,
        recorded_at=NOW,
        total_amount=sum(p.debit_amount for p in postings),
    )
    tx.postings = postings
    return tx


def accounts_result(*accounts: Account) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(accounts)
    return result


# ---------------------------------------------------------------------------
# build_formance_postings — pairing
# ---------------------------------------------------------------------------


def test_two_leg_pairing() -> None:
    """A balanced 2-leg entry emits exactly ONE paired Formance posting."""
    dr = make_account("4000")
    cr = make_account("2000")
    postings = [posting_create(dr, debit=10000), posting_create(cr, credit=10000)]

    result = build_formance_postings(postings, codes=codes_for(dr, cr))

    assert result == [
        {
            "source": "2000",
            "destination": "4000",
            "amount": "100.00",
            "asset": "GBP/2",
        }
    ]


def test_compound_one_debit_two_credits() -> None:
    """1 Dr + 2 Cr legs: debit amount is split across the credits in order."""
    dr = make_account("4000")
    cr1 = make_account("2000")
    cr2 = make_account("2100")
    postings = [
        posting_create(dr, debit=100),
        posting_create(cr1, credit=60),
        posting_create(cr2, credit=40),
    ]

    result = build_formance_postings(postings, codes=codes_for(dr, cr1, cr2))

    assert result == [
        {"source": "2000", "destination": "4000", "amount": "0.60", "asset": "GBP/2"},
        {"source": "2100", "destination": "4000", "amount": "0.40", "asset": "GBP/2"},
    ]


def test_compound_two_debits_one_credit() -> None:
    """2 Dr + 1 Cr legs: contract example — credit splits across debits."""
    dr1 = make_account("4000")
    dr2 = make_account("4100")
    cr = make_account("2000")
    postings = [
        posting_create(dr1, debit=100),
        posting_create(dr2, debit=50),
        posting_create(cr, credit=150),
    ]

    result = build_formance_postings(postings, codes=codes_for(dr1, dr2, cr))

    assert result == [
        {"source": "2000", "destination": "4000", "amount": "1.00", "asset": "GBP/2"},
        {"source": "2000", "destination": "4100", "amount": "0.50", "asset": "GBP/2"},
    ]


def test_compound_unequal_chunks() -> None:
    """2 Dr + 2 Cr with unequal amounts: greedy chunking in posting order."""
    dr1 = make_account("4000")
    dr2 = make_account("4100")
    cr1 = make_account("2000")
    cr2 = make_account("2100")
    postings = [
        posting_create(dr1, debit=100),
        posting_create(dr2, debit=25),
        posting_create(cr1, credit=75),
        posting_create(cr2, credit=50),
    ]

    result = build_formance_postings(postings, codes=codes_for(dr1, dr2, cr1, cr2))

    assert result == [
        {"source": "2000", "destination": "4000", "amount": "0.75", "asset": "GBP/2"},
        {"source": "2100", "destination": "4000", "amount": "0.25", "asset": "GBP/2"},
        {"source": "2100", "destination": "4100", "amount": "0.25", "asset": "GBP/2"},
    ]
    total = sum(int(p["amount"].replace(".", "")) for p in result)
    assert total == 125  # sum of debits in pence


def test_currency_in_asset() -> None:
    """Asset reflects the transaction currency."""
    dr = make_account("4000")
    cr = make_account("2000")
    postings = [posting_create(dr, debit=1), posting_create(cr, credit=1)]

    result = build_formance_postings(postings, currency="USD", codes=codes_for(dr, cr))

    assert result[0]["asset"] == "USD/2"


def test_currency_defaults_to_gbp() -> None:
    dr = make_account("4000")
    cr = make_account("2000")
    postings = [posting_create(dr, debit=1), posting_create(cr, credit=1)]

    result = build_formance_postings(postings, codes=codes_for(dr, cr))

    assert result[0]["asset"] == "GBP/2"


# ---------------------------------------------------------------------------
# build_formance_postings — exact decimal conversion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pence,expected",
    [
        (1, "0.01"),            # £0.01
        (10, "0.10"),
        (123, "1.23"),
        (5000, "50.00"),
        (123456, "1234.56"),    # £1234.56
        (999999999999, "9999999999.99"),  # large amount
        (123456789012, "1234567890.12"),
    ],
)
def test_exact_decimal_conversion(pence: int, expected: str) -> None:
    dr = make_account("4000")
    cr = make_account("2000")
    postings = [posting_create(dr, debit=pence), posting_create(cr, credit=pence)]

    result = build_formance_postings(postings, codes=codes_for(dr, cr))

    assert len(result) == 1
    assert result[0]["amount"] == expected
    assert isinstance(result[0]["amount"], str)


# ---------------------------------------------------------------------------
# build_formance_postings — no "world", unbalanced/zero
# ---------------------------------------------------------------------------


def test_world_never_emitted() -> None:
    """'world' must never appear as source or destination."""
    dr1 = make_account("4000")
    dr2 = make_account("4100")
    cr1 = make_account("2000")
    cr2 = make_account("2100")
    postings = [
        posting_create(dr1, debit=100),
        posting_create(dr2, debit=25),
        posting_create(cr1, credit=75),
        posting_create(cr2, credit=50),
    ]

    result = build_formance_postings(postings, codes=codes_for(dr1, dr2, cr1, cr2))

    assert len(result) == 3
    for p in result:
        assert p["source"] != "world"
        assert p["destination"] != "world"
        assert "world" not in p["source"]
        assert "world" not in p["destination"]


def test_unbalanced_raises() -> None:
    dr = make_account("4000")
    cr = make_account("2000")
    postings = [posting_create(dr, debit=100), posting_create(cr, credit=99)]

    with pytest.raises(ValueError, match="Unbalanced"):
        build_formance_postings(postings, codes=codes_for(dr, cr))


def test_zero_zero_rejected_upstream_by_validator() -> None:
    """PostingCreate itself rejects entries with no positive amount."""
    with pytest.raises(ValidationError):
        PostingCreate(account_id=uuid.uuid4(), debit_amount=0, credit_amount=0)


def test_orm_postings_duck_typed() -> None:
    """The helper also accepts ORM Posting objects (duck typing)."""
    dr = make_account("4000")
    cr = make_account("2000")
    postings = [orm_posting(dr, debit=10000), orm_posting(cr, credit=10000)]

    result = build_formance_postings(postings, codes=codes_for(dr, cr))

    assert result == [
        {
            "source": "2000",
            "destination": "4000",
            "amount": "100.00",
            "asset": "GBP/2",
        }
    ]


# ---------------------------------------------------------------------------
# _post_to_formance — full path
# ---------------------------------------------------------------------------


async def test_post_to_formance_mirrors_with_reference_and_metadata(monkeypatch) -> None:
    dr = make_account("4000")
    cr = make_account("2000")
    tx = make_transaction(
        [dr, cr],
        [orm_posting(dr, debit=10000), orm_posting(cr, credit=10000)],
    )

    db = AsyncMock()
    db.execute = AsyncMock(return_value=accounts_result(dr, cr))
    mock_create = AsyncMock(return_value={"txid": 42})

    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    with patch(
        "src.services.transaction_service.formance_client.create_transaction",
        mock_create,
    ):
        result = await TransactionService._post_to_formance(db, tx)

    assert result == {"txid": 42}
    mock_create.assert_called_once()
    kwargs = mock_create.call_args.kwargs
    assert kwargs["ledger"] == "main"
    assert kwargs["reference"] == "JE-2026-0042"
    assert kwargs["postings"] == [
        {
            "source": "2000",
            "destination": "4000",
            "amount": "100.00",
            "asset": "GBP/2",
        }
    ]
    assert kwargs["metadata"]["transaction_id"] == str(tx.id)
    assert kwargs["metadata"]["description"] == "Sales invoice"


async def test_post_to_formance_unset_env_returns_none(monkeypatch) -> None:
    dr = make_account("4000")
    cr = make_account("2000")
    tx = make_transaction(
        [dr, cr],
        [orm_posting(dr, debit=10000), orm_posting(cr, credit=10000)],
    )

    db = AsyncMock()
    monkeypatch.delenv("FORNANCE_LEDGER_URL", raising=False)
    with patch(
        "src.services.transaction_service.formance_client.create_transaction",
        AsyncMock(),
    ) as mock_create:
        result = await TransactionService._post_to_formance(db, tx)

    assert result is None
    db.execute.assert_not_called()
    mock_create.assert_not_called()


async def test_post_to_formance_http_error_returns_none(monkeypatch) -> None:
    dr = make_account("4000")
    cr = make_account("2000")
    tx = make_transaction(
        [dr, cr],
        [orm_posting(dr, debit=10000), orm_posting(cr, credit=10000)],
    )

    db = AsyncMock()
    db.execute = AsyncMock(return_value=accounts_result(dr, cr))

    def boom(*args, **kwargs):
        raise httpx.HTTPStatusError(
            "Server error",
            request=httpx.Request("POST", "http://formance:3068/main/transactions"),
            response=httpx.Response(500),
        )

    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    with patch(
        "src.services.transaction_service.formance_client.create_transaction",
        AsyncMock(side_effect=boom),
    ):
        result = await TransactionService._post_to_formance(db, tx)

    assert result is None


async def test_post_to_formance_connection_error_returns_none(monkeypatch) -> None:
    dr = make_account("4000")
    cr = make_account("2000")
    tx = make_transaction(
        [dr, cr],
        [orm_posting(dr, debit=10000), orm_posting(cr, credit=10000)],
    )

    db = AsyncMock()
    db.execute = AsyncMock(return_value=accounts_result(dr, cr))

    def boom(*args, **kwargs):
        raise httpx.ConnectError("Connection refused", request=httpx.Request("POST", "http://x"))

    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    with patch(
        "src.services.transaction_service.formance_client.create_transaction",
        AsyncMock(side_effect=boom),
    ):
        result = await TransactionService._post_to_formance(db, tx)

    assert result is None


# ---------------------------------------------------------------------------
# reverse_transaction — mirroring the reversing entry
# ---------------------------------------------------------------------------


def _mock_result(return_value):
    """Create a MagicMock that mimics an AsyncResult."""
    m = MagicMock()
    m.scalar_one_or_none.return_value = return_value
    m.scalars.return_value.all.return_value = (
        return_value if isinstance(return_value, list) else [return_value]
    )
    m.scalar_one.return_value = (
        1 if return_value is None else (len(return_value) if isinstance(return_value, list) else 1)
    )
    return m


async def test_reverse_transaction_mirrors_reversal(monkeypatch) -> None:
    """reverse_transaction must mirror the reversing entry to Formance.

    Original: Dr 4000 £100.00 / Cr 2000 £100.00.
    Reversing entry swaps sides, so the mirror must emit
    {source: 4000, destination: 2000} — the exact inverse of the original.
    """
    dr = make_account("4000")
    cr = make_account("2000")

    tx_id = uuid.uuid4()
    original = Transaction(
        id=tx_id,
        reference="JE-2026-0001",
        description="Original",
        currency="GBP",
        status="posted",
        effective_date=None,
        total_amount=10000,
    )
    original.postings = [
        Posting(
            id=uuid.uuid4(),
            transaction_id=tx_id,
            account_id=dr.id,
            debit_amount=10000,
            credit_amount=0,
        ),
        Posting(
            id=uuid.uuid4(),
            transaction_id=tx_id,
            account_id=cr.id,
            debit_amount=0,
            credit_amount=10000,
        ),
    ]

    db = AsyncMock()
    added_postings: list[Posting] = []

    def track_add(obj) -> None:
        if isinstance(obj, Posting):
            added_postings.append(obj)

    async def mock_refresh(obj, attribute_names=None) -> None:
        # Real AsyncSession.refresh reloads the postings collection from the
        # DB after commit; mirror that here so _post_to_formance sees them.
        if attribute_names and "postings" in attribute_names:
            obj.postings = added_postings

    db.add = MagicMock(side_effect=track_add)
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock(side_effect=mock_refresh)
    db.execute = AsyncMock(
        side_effect=[
            _mock_result(original),      # load original
            _mock_result(None),          # JE reference generation
            accounts_result(dr, cr),     # _post_to_formance account lookup
        ]
    )

    mock_create = AsyncMock(return_value={"txid": 7})
    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    with patch(
        "src.services.transaction_service.formance_client.create_transaction",
        mock_create,
    ):
        reversing = await TransactionService.reverse_transaction(db, tx_id)

    assert original.status == "reversed"
    assert reversing.status == "posted"
    assert reversing.reference == "JE-2026-0001"

    mock_create.assert_called_once()
    kwargs = mock_create.call_args.kwargs
    assert kwargs["postings"] == [
        {
            "source": "4000",   # credit leg of the reversal (original debit)
            "destination": "2000",  # debit leg of the reversal (original credit)
            "amount": "100.00",
            "asset": "GBP/2",
        }
    ]
    assert kwargs["reference"] == "JE-2026-0001"
    assert kwargs["metadata"]["transaction_id"] == str(reversing.id)
    for p in kwargs["postings"]:
        assert p["source"] != "world"
        assert p["destination"] != "world"


async def test_reverse_transaction_skips_mirror_when_env_unset(monkeypatch) -> None:
    """No Formance call when FORNANCE_LEDGER_URL is unset."""
    dr = make_account("4000")
    cr = make_account("2000")

    tx_id = uuid.uuid4()
    original = Transaction(
        id=tx_id,
        reference="JE-2026-0002",
        description="Original",
        currency="GBP",
        status="posted",
        effective_date=None,
        total_amount=5000,
    )
    original.postings = [
        Posting(
            id=uuid.uuid4(),
            transaction_id=tx_id,
            account_id=dr.id,
            debit_amount=5000,
            credit_amount=0,
        ),
        Posting(
            id=uuid.uuid4(),
            transaction_id=tx_id,
            account_id=cr.id,
            debit_amount=0,
            credit_amount=5000,
        ),
    ]

    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _mock_result(original),
            _mock_result(None),
        ]
    )

    monkeypatch.delenv("FORNANCE_LEDGER_URL", raising=False)
    with patch(
        "src.services.transaction_service.formance_client.create_transaction",
        AsyncMock(),
    ) as mock_create:
        reversing = await TransactionService.reverse_transaction(db, tx_id)

    assert reversing.status == "posted"
    mock_create.assert_not_called()


async def test_mirror_timestamp_uses_effective_date_not_wall_clock(monkeypatch) -> None:
    """The Formance mirror timestamp must be the transaction's EFFECTIVE
    date — the report engine filters periods on it, and the Postgres
    cross-check filters effective_date; a wall-clock (recorded_at)
    timestamp makes the two sides disagree for back-dated entries."""
    dr = make_account("1000")
    cr = make_account("5000")
    tx = Transaction(
        id=uuid.uuid4(),
        reference="JE-2026-0001",
        description="test",
        currency="GBP",
        status="posted",
        effective_date=date(2024, 1, 15),
        total_amount=5000,
    )
    tx.postings = [
        Posting(id=uuid.uuid4(), transaction_id=tx.id, account_id=dr.id,
                debit_amount=5000, credit_amount=0),
        Posting(id=uuid.uuid4(), transaction_id=tx.id, account_id=cr.id,
                debit_amount=0, credit_amount=5000),
    ]
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[accounts_result(dr, cr)])
    db.refresh = AsyncMock()

    posted: list[dict] = []

    async def fake_create(ledger, postings, reference=None, timestamp=None, metadata=None):
        posted.append({"timestamp": timestamp, "metadata": metadata})
        return {"txid": 1}

    monkeypatch.setenv("FORNANCE_LEDGER_URL", "http://formance:3068")
    with patch(
        "src.services.transaction_service.formance_client.create_transaction",
        new=fake_create,
    ):
        result = await TransactionService._post_to_formance(db, tx)

    assert result is not None
    assert posted[0]["timestamp"] == "2024-01-15T00:00:00Z"
