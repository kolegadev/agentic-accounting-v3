"""Unit tests for the ledger parity verifier (Contract F2).

The Formance client is mocked at the ``src.services.ledger_parity.formance_client``
boundary and the database is a duck-typed AsyncSession stand-in that serves the
exact queries ``verify_parity`` issues.  Coverage:

  * perfect parity -> ok=True
  * every drift type: missing mirror, orphan, leg amount mismatch, wrong
    account code, reference mismatch, balance mismatch
  * drafts excluded from both the transaction-level and balance-level checks
  * unreachable Formance -> ok=False with an error field, never a raise
  * pagination across multiple list_transactions pages
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src.models.account import Account
from src.models.transaction import Posting, Transaction
from src.services.ledger_parity import ParityReport, verify_parity

GBP = "GBP/2"


# ---------------------------------------------------------------------------
# Model builders
# ---------------------------------------------------------------------------


def make_account(code: str, *, is_active: bool = True) -> Account:
    return Account(
        id=uuid.uuid4(),
        code=code,
        name=f"Account {code}",
        category="Asset",
        type="Bank",
        is_active=is_active,
    )


def make_posting(account: Account, *, debit: int = 0, credit: int = 0) -> Posting:
    return Posting(
        id=uuid.uuid4(),
        account_id=account.id,
        debit_amount=debit,
        credit_amount=credit,
    )


def make_transaction(
    reference: str,
    status: str,
    postings: list[Posting],
    *,
    currency: str = "GBP",
) -> Transaction:
    tx = Transaction(
        id=uuid.uuid4(),
        reference=reference,
        status=status,
        currency=currency,
        total_amount=sum(p.debit_amount for p in postings),
    )
    tx.postings = list(postings)
    return tx


def amount(pence: int) -> str:
    """Pence -> exact major-unit decimal string (e.g. 7001 -> '70.01')."""
    return str(Decimal(pence).scaleb(-2))


# ---------------------------------------------------------------------------
# Fake AsyncSession
# ---------------------------------------------------------------------------


class FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = list(rows)

    def scalars(self) -> "FakeResult":
        return self

    def all(self) -> list:
        return self._rows


class FakeSession:
    """Answers only the two queries verify_parity issues."""

    def __init__(self, transactions=(), accounts=()) -> None:
        self._transactions = list(transactions)
        self._accounts = list(accounts)

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Transaction:
            rows = [t for t in self._transactions if self._status_ok(stmt, t)]
            return FakeResult(rows)
        if entity is Account:
            return FakeResult(self._accounts)
        raise AssertionError(f"Unexpected statement: {stmt}")

    @staticmethod
    def _status_ok(stmt, tx) -> bool:
        """Honour a ``Transaction.status.in_(...)`` WHERE clause if present."""
        for crit in getattr(stmt, "_where_criteria", ()) or ():
            if (
                hasattr(crit, "left")
                and getattr(crit.left, "key", None) == "status"
                and hasattr(crit.right, "value")
            ):
                return tx.status in list(crit.right.value)
        return True


# ---------------------------------------------------------------------------
# Formance payload helpers
# ---------------------------------------------------------------------------


def f_tx(txid: int, reference: str, tx_id: str, postings: list[dict]) -> dict:
    return {
        "txid": txid,
        "reference": reference,
        "metadata": {"transaction_id": tx_id, "description": ""},
        "postings": postings,
    }


def list_response(*transactions: dict) -> dict:
    return {"cursor": {"hasMore": False}, "data": list(transactions)}


def balances_response(*pairs: tuple[str, int]) -> dict:
    return {
        "data": [
            {"address": code, "balances": {GBP: amount(pence)}}
            for code, pence in pairs
        ]
    }


def client_mock(list_result=None, balances_result=None):
    mock = MagicMock()
    mock.list_transactions = AsyncMock(return_value=list_result)
    mock.get_balances = AsyncMock(return_value=balances_result)
    return mock


async def run_verify(db: FakeSession, client) -> ParityReport:
    with patch("src.services.ledger_parity.formance_client", client):
        return await verify_parity(db)


# ---------------------------------------------------------------------------
# Perfect parity
# ---------------------------------------------------------------------------


async def test_perfect_parity_reports_ok() -> None:
    bank = make_account("1000")
    revenue = make_account("4000")
    tx1 = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=10000), make_posting(revenue, credit=10000)],
    )
    tx2 = make_transaction(
        "JE-2026-0002",
        "posted",
        [make_posting(revenue, debit=3000), make_posting(bank, credit=3000)],
    )
    db = FakeSession(transactions=[tx1, tx2], accounts=[bank, revenue])

    mirrors = list_response(
        f_tx(
            1,
            "JE-2026-0001",
            str(tx1.id),
            [{"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}],
        ),
        f_tx(
            2,
            "JE-2026-0002",
            str(tx2.id),
            [{"source": "1000", "destination": "4000", "amount": "30.00", "asset": GBP}],
        ),
    )
    balances = balances_response(("1000", 7000), ("4000", -7000))
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is True
    assert report.error is None
    assert report.missing_in_formance == []
    assert report.orphans_in_formance == []
    assert report.leg_mismatches == []
    assert report.balance_mismatches == []
    assert "OK" in report.summary


async def test_compound_transaction_perfect_parity() -> None:
    """A compound entry (1 Dr, 2 Cr) mirrors as two paired postings and passes."""
    dr = make_account("4000")
    cr1 = make_account("2000")
    cr2 = make_account("2100")
    tx = make_transaction(
        "JE-2026-0007",
        "posted",
        [
            make_posting(dr, debit=100),
            make_posting(cr1, credit=60),
            make_posting(cr2, credit=40),
        ],
    )
    db = FakeSession(transactions=[tx], accounts=[dr, cr1, cr2])

    mirrors = list_response(
        f_tx(
            7,
            "JE-2026-0007",
            str(tx.id),
            [
                {"source": "2000", "destination": "4000", "amount": "0.60", "asset": GBP},
                {"source": "2100", "destination": "4000", "amount": "0.40", "asset": GBP},
            ],
        )
    )
    balances = balances_response(("4000", 100), ("2000", -60), ("2100", -40))
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is True
    assert report.leg_mismatches == []
    assert report.balance_mismatches == []


# ---------------------------------------------------------------------------
# Missing mirror
# ---------------------------------------------------------------------------


async def test_missing_mirror_flagged() -> None:
    bank = make_account("1000")
    revenue = make_account("4000")
    tx1 = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=10000), make_posting(revenue, credit=10000)],
    )
    tx2 = make_transaction(
        "JE-2026-0002",
        "posted",
        [make_posting(revenue, debit=3000), make_posting(bank, credit=3000)],
    )
    db = FakeSession(transactions=[tx1, tx2], accounts=[bank, revenue])

    mirrors = list_response(
        f_tx(
            1,
            "JE-2026-0001",
            str(tx1.id),
            [{"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}],
        )
    )
    balances = balances_response(("1000", 7000), ("4000", -7000))
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert report.missing_in_formance == [
        {
            "kind": "missing_mirror",
            "transaction_id": str(tx2.id),
            "reference": "JE-2026-0002",
            "expected": str(tx2.id),
            "actual": None,
        }
    ]
    assert report.orphans_in_formance == []
    assert report.leg_mismatches == []
    assert report.balance_mismatches == []
    assert "FAILED" in report.summary


# ---------------------------------------------------------------------------
# Orphan / foreign Formance rows
# ---------------------------------------------------------------------------


async def test_orphan_flagged() -> None:
    bank = make_account("1000")
    revenue = make_account("4000")
    tx = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=10000), make_posting(revenue, credit=10000)],
    )
    db = FakeSession(transactions=[tx], accounts=[bank, revenue])

    rogue_id = uuid.uuid4()
    mirrors = list_response(
        f_tx(
            1,
            "JE-2026-0001",
            str(tx.id),
            [{"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}],
        ),
        f_tx(
            99,
            "JE-1999-9999",
            str(rogue_id),
            [{"source": "9999", "destination": "8888", "amount": "1.00", "asset": GBP}],
        ),
    )
    balances = balances_response(("1000", 10000), ("4000", -10000))
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert report.orphans_in_formance == [
        {
            "kind": "orphan",
            "transaction_id": str(rogue_id),
            "reference": "JE-1999-9999",
            "formance_txid": 99,
            "expected": None,
            "actual": {"txid": 99, "reference": "JE-1999-9999"},
        }
    ]
    assert report.missing_in_formance == []
    assert report.leg_mismatches == []
    assert report.balance_mismatches == []


async def test_formance_row_without_metadata_is_orphan() -> None:
    bank = make_account("1000")
    revenue = make_account("4000")
    tx = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=10000), make_posting(revenue, credit=10000)],
    )
    db = FakeSession(transactions=[tx], accounts=[bank, revenue])

    mirrors = list_response(
        {
            "txid": 55,
            "reference": "JE-1990-0001",
            "metadata": {"description": "no transaction_id"},
            "postings": [{"source": "9999", "destination": "8888", "amount": "1.00", "asset": GBP}],
        }
    )
    balances = balances_response()
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert len(report.orphans_in_formance) == 1
    assert report.orphans_in_formance[0]["transaction_id"] is None
    assert report.orphans_in_formance[0]["formance_txid"] == 55


# ---------------------------------------------------------------------------
# Leg-level drifts
# ---------------------------------------------------------------------------


async def test_leg_amount_mismatch_flagged() -> None:
    bank = make_account("1000")
    revenue = make_account("4000")
    tx = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=10000), make_posting(revenue, credit=10000)],
    )
    db = FakeSession(transactions=[tx], accounts=[bank, revenue])

    mirrors = list_response(
        f_tx(
            1,
            "JE-2026-0001",
            str(tx.id),
            [{"source": "4000", "destination": "1000", "amount": "99.99", "asset": GBP}],
        )
    )
    balances = balances_response(("1000", 10000), ("4000", -10000))
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert report.leg_mismatches == [
        {
            "kind": "postings",
            "transaction_id": str(tx.id),
            "reference": "JE-2026-0001",
            "expected": [
                {"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}
            ],
            "actual": [
                {"source": "4000", "destination": "1000", "amount": "99.99", "asset": GBP}
            ],
        }
    ]
    assert report.missing_in_formance == []
    assert report.orphans_in_formance == []
    assert report.balance_mismatches == []


async def test_wrong_account_code_flagged() -> None:
    bank = make_account("1000")
    revenue = make_account("4000")
    tx = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=10000), make_posting(revenue, credit=10000)],
    )
    db = FakeSession(transactions=[tx], accounts=[bank, revenue])

    mirrors = list_response(
        f_tx(
            1,
            "JE-2026-0001",
            str(tx.id),
            [{"source": "4000", "destination": "9999", "amount": "100.00", "asset": GBP}],
        )
    )
    balances = balances_response(("1000", 10000), ("4000", -10000))
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert len(report.leg_mismatches) == 1
    entry = report.leg_mismatches[0]
    assert entry["kind"] == "postings"
    assert entry["transaction_id"] == str(tx.id)
    assert entry["expected"][0]["destination"] == "1000"
    assert entry["actual"][0]["destination"] == "9999"
    assert report.balance_mismatches == []


async def test_reference_mismatch_flagged() -> None:
    bank = make_account("1000")
    revenue = make_account("4000")
    tx = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=10000), make_posting(revenue, credit=10000)],
    )
    db = FakeSession(transactions=[tx], accounts=[bank, revenue])

    mirrors = list_response(
        f_tx(
            1,
            "JE-2025-7777",
            str(tx.id),
            [{"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}],
        )
    )
    balances = balances_response(("1000", 10000), ("4000", -10000))
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert report.leg_mismatches == [
        {
            "kind": "reference",
            "transaction_id": str(tx.id),
            "reference": "JE-2026-0001",
            "expected": "JE-2026-0001",
            "actual": "JE-2025-7777",
        }
    ]
    assert report.balance_mismatches == []


# ---------------------------------------------------------------------------
# Balance-level drift
# ---------------------------------------------------------------------------


async def test_balance_mismatch_flagged() -> None:
    bank = make_account("1000")
    revenue = make_account("4000")
    tx1 = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=10000), make_posting(revenue, credit=10000)],
    )
    tx2 = make_transaction(
        "JE-2026-0002",
        "posted",
        [make_posting(revenue, debit=3000), make_posting(bank, credit=3000)],
    )
    db = FakeSession(transactions=[tx1, tx2], accounts=[bank, revenue])

    mirrors = list_response(
        f_tx(
            1,
            "JE-2026-0001",
            str(tx1.id),
            [{"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}],
        ),
        f_tx(
            2,
            "JE-2026-0002",
            str(tx2.id),
            [{"source": "1000", "destination": "4000", "amount": "30.00", "asset": GBP}],
        ),
    )
    balances = balances_response(("1000", 7001), ("4000", -7000))  # 1000 off by 1p
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert report.balance_mismatches == [
        {
            "kind": "balance",
            "account_id": str(bank.id),
            "account_code": "1000",
            "expected": 7000,
            "actual": 7001,
        }
    ]
    assert report.missing_in_formance == []
    assert report.orphans_in_formance == []
    assert report.leg_mismatches == []


async def test_inactive_account_not_checked() -> None:
    bank = make_account("1000")
    dormant = make_account("5210", is_active=False)
    tx = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=10000), make_posting(dormant, credit=10000)],
    )
    db = FakeSession(transactions=[tx], accounts=[bank, dormant])

    mirrors = list_response(
        f_tx(
            1,
            "JE-2026-0001",
            str(tx.id),
            [{"source": "5210", "destination": "1000", "amount": "100.00", "asset": GBP}],
        )
    )
    # Formance reports a wildly wrong balance for the dormant account:
    # it must be ignored because the account is inactive.
    balances = balances_response(("1000", 10000), ("5210", 123456789))
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is True
    assert report.balance_mismatches == []


# ---------------------------------------------------------------------------
# Drafts excluded on both sides
# ---------------------------------------------------------------------------


async def test_draft_excluded_from_both_checks() -> None:
    bank = make_account("1000")
    revenue = make_account("4000")
    draft = make_transaction(
        "JE-2026-0001",  # reference irrelevant: drafts are never mirrored
        "draft",
        [make_posting(bank, debit=50000), make_posting(revenue, credit=50000)],
    )
    posted = make_transaction(
        "JE-2026-0002",
        "posted",
        [make_posting(bank, debit=10000), make_posting(revenue, credit=10000)],
    )
    db = FakeSession(transactions=[draft, posted], accounts=[bank, revenue])

    mirrors = list_response(
        f_tx(
            1,
            "JE-2026-0002",
            str(posted.id),
            [{"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}],
        )
    )
    # Balances reflect only the posted transaction.  If the draft's £500 legs
    # leaked into either check, the report would flag a missing mirror or a
    # balance mismatch.
    balances = balances_response(("1000", 10000), ("4000", -10000))
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is True
    assert report.missing_in_formance == []
    assert report.balance_mismatches == []


# ---------------------------------------------------------------------------
# Unreachable Formance — error field, never raise
# ---------------------------------------------------------------------------


async def test_formance_unreachable_reports_error_no_raise() -> None:
    bank = make_account("1000")
    revenue = make_account("4000")
    tx = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=10000), make_posting(revenue, credit=10000)],
    )
    db = FakeSession(transactions=[tx], accounts=[bank, revenue])

    client = MagicMock()
    client.list_transactions = AsyncMock(
        side_effect=httpx.ConnectError(
            "Connection refused",
            request=httpx.Request("GET", "http://formance:3068/main/transactions"),
        )
    )

    report = await run_verify(db, client)  # must not raise

    assert isinstance(report, ParityReport)
    assert report.ok is False
    assert report.error is not None
    assert "Connection refused" in report.error
    assert "FAILED" in report.summary
    assert report.missing_in_formance == []
    assert report.orphans_in_formance == []
    assert report.leg_mismatches == []
    assert report.balance_mismatches == []
    client.get_balances.assert_not_called()


async def test_balances_unreachable_reports_error_no_raise() -> None:
    bank = make_account("1000")
    revenue = make_account("4000")
    tx = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=10000), make_posting(revenue, credit=10000)],
    )
    db = FakeSession(transactions=[tx], accounts=[bank, revenue])

    client = MagicMock()
    client.list_transactions = AsyncMock(
        return_value=list_response(
            f_tx(
                1,
                "JE-2026-0001",
                str(tx.id),
                [{"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}],
            )
        )
    )
    client.get_balances = AsyncMock(
        side_effect=httpx.ReadTimeout(
            "timed out", request=httpx.Request("GET", "http://formance:3068/main/balances")
        )
    )

    report = await run_verify(db, client)  # must not raise

    assert report.ok is False
    assert report.error is not None
    assert "timed out" in report.error


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


async def test_pagination_accumulates_pages() -> None:
    bank = make_account("1000")
    revenue = make_account("4000")
    tx1 = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=10000), make_posting(revenue, credit=10000)],
    )
    tx2 = make_transaction(
        "JE-2026-0002",
        "posted",
        [make_posting(revenue, debit=3000), make_posting(bank, credit=3000)],
    )
    db = FakeSession(transactions=[tx1, tx2], accounts=[bank, revenue])

    client = MagicMock()
    client.list_transactions = AsyncMock(
        side_effect=[
            {
                "cursor": {"hasMore": True, "next": "1"},
                "data": [
                    f_tx(
                        1,
                        "JE-2026-0001",
                        str(tx1.id),
                        [
                            {
                                "source": "4000",
                                "destination": "1000",
                                "amount": "100.00",
                                "asset": GBP,
                            }
                        ],
                    )
                ],
            },
            {
                "cursor": {"hasMore": False},
                "data": [
                    f_tx(
                        2,
                        "JE-2026-0002",
                        str(tx2.id),
                        [
                            {
                                "source": "1000",
                                "destination": "4000",
                                "amount": "30.00",
                                "asset": GBP,
                            }
                        ],
                    )
                ],
            },
        ]
    )
    client.get_balances = AsyncMock(
        return_value=balances_response(("1000", 7000), ("4000", -7000))
    )

    report = await run_verify(db, client)

    assert report.ok is True
    assert report.missing_in_formance == []
    assert report.orphans_in_formance == []
    assert client.list_transactions.call_count == 2
    assert client.list_transactions.call_args_list[0].kwargs["after"] is None
    assert client.list_transactions.call_args_list[1].kwargs["after"] == "1"
