"""Unit tests for the ledger parity verifier (Contracts F2 + F13).

The Formance client is mocked at the ``src.services.ledger_parity.formance_client``
boundary and the database is a duck-typed AsyncSession stand-in that serves the
exact queries ``verify_parity`` issues.  Coverage:

  * perfect parity -> ok=True (all finding lists empty)
  * every drift type: missing mirror, orphan, leg amount mismatch, wrong
    account code, reference mismatch, balance mismatch
  * drafts excluded from both the transaction-level and balance-level checks
  * unreachable Formance -> ok=False with an error field, never a raise
  * pagination across multiple list_transactions pages
  * F13 hardening: malformed balance payloads, missing balance addresses,
    inactive accounts with postings, duplicate Formance rows, foreign
    balance addresses, sub-penny amounts, mixed assets
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.models.account import Account
from src.models.transaction import Posting, Transaction
from src.services.ledger_parity import (
    ParityReport,
    _decimal_to_pence,
    _parse_balances,
    verify_parity,
)

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
    assert report.parse_errors == []
    assert report.duplicate_rows == []
    assert report.foreign_addresses == []
    assert report.foreign_assets == []
    assert report.missing_balance_addresses == []
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


async def test_inactive_account_with_postings_drift_flagged() -> None:
    """F13 gap 2: an inactive account WITH in-scope postings is compared."""
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
    # Formance reports a wildly wrong balance for the dormant account —
    # previously invisible because only is_active accounts were compared.
    balances = balances_response(("1000", 10000), ("5210", 123456789))
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert report.balance_mismatches == [
        {
            "kind": "balance",
            "account_id": str(dormant.id),
            "account_code": "5210",
            "expected": -10000,
            "actual": 123456789,
        }
    ]


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


# ---------------------------------------------------------------------------
# Contract F13 — hardening (the 7 F2 verifier gaps)
# ---------------------------------------------------------------------------

# Gap 1: malformed balances / missing expected addresses


async def test_malformed_balance_payload_is_parse_error_not_zero() -> None:
    """Garbage balance value on a zero-net ledger is a parse error, not 0."""
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
        [make_posting(revenue, debit=10000), make_posting(bank, credit=10000)],
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
            [{"source": "1000", "destination": "4000", "amount": "100.00", "asset": GBP}],
        ),
    )
    balances = {
        "data": [
            {"address": "1000", "balances": {GBP: "not-a-number"}},
            {"address": "4000", "balances": {GBP: "0.00"}},
        ]
    }
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert len(report.parse_errors) == 1
    entry = report.parse_errors[0]
    assert entry["kind"] == "parse_error"
    assert entry["address"] == "1000"
    assert entry["asset"] == GBP
    assert entry["raw"] == "not-a-number"
    assert entry["error"]
    assert report.balance_mismatches == []
    assert "parse errors" in report.summary


async def test_missing_expected_balance_address_flagged() -> None:
    """An address the mirror postings touch but get_balances omits is flagged."""
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
            [{"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}],
        )
    )
    balances = balances_response(("4000", -10000))  # "1000" omitted entirely
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert report.missing_balance_addresses == [
        {"kind": "missing_balance", "address": "1000"}
    ]
    # The absent address is NOT silently read as 0: no fabricated mismatch.
    assert report.balance_mismatches == []
    assert "missing balance addresses" in report.summary


async def test_missing_balance_address_on_zero_net_ledger_flips_ok() -> None:
    """The F2 false-ok: zero-net ledger + absent address must not report ok."""
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
        [make_posting(revenue, debit=10000), make_posting(bank, credit=10000)],
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
            [{"source": "1000", "destination": "4000", "amount": "100.00", "asset": GBP}],
        ),
    )
    balances = balances_response()  # both expected addresses absent
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert report.missing_balance_addresses == [
        {"kind": "missing_balance", "address": "1000"},
        {"kind": "missing_balance", "address": "4000"},
    ]
    assert report.balance_mismatches == []


# Gap 2: inactive accounts with postings


async def test_inactive_account_with_postings_in_parity_ok() -> None:
    """An inactive account with postings and a correct balance passes."""
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
    balances = balances_response(("1000", 10000), ("5210", -10000))
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is True
    assert report.balance_mismatches == []


async def test_inactive_account_without_postings_ignored() -> None:
    """An inactive account with no postings and no balance is not compared."""
    bank = make_account("1000")
    revenue = make_account("4000")
    dormant = make_account("5210", is_active=False)
    tx = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=10000), make_posting(revenue, credit=10000)],
    )
    db = FakeSession(transactions=[tx], accounts=[bank, revenue, dormant])

    mirrors = list_response(
        f_tx(
            1,
            "JE-2026-0001",
            str(tx.id),
            [{"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}],
        )
    )
    balances = balances_response(("1000", 10000), ("4000", -10000))
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is True
    assert report.balance_mismatches == []


async def test_inactive_account_nonzero_balance_without_postings_flagged() -> None:
    """An inactive account Formance holds a nonzero balance for is compared."""
    bank = make_account("1000")
    revenue = make_account("4000")
    dormant = make_account("5210", is_active=False)
    tx = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=10000), make_posting(revenue, credit=10000)],
    )
    db = FakeSession(transactions=[tx], accounts=[bank, revenue, dormant])

    mirrors = list_response(
        f_tx(
            1,
            "JE-2026-0001",
            str(tx.id),
            [{"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}],
        )
    )
    balances = balances_response(
        ("1000", 10000), ("4000", -10000), ("5210", 12345)
    )
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert report.balance_mismatches == [
        {
            "kind": "balance",
            "account_id": str(dormant.id),
            "account_code": "5210",
            "expected": 0,
            "actual": 12345,
        }
    ]


# Gaps 3+4: duplicate Formance rows


async def test_duplicate_formance_rows_flagged() -> None:
    """Two rows claiming one transaction_id -> duplicate_rows, both txids."""
    bank = make_account("1000")
    revenue = make_account("4000")
    tx = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=10000), make_posting(revenue, credit=10000)],
    )
    db = FakeSession(transactions=[tx], accounts=[bank, revenue])

    posting = {"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}
    mirrors = list_response(
        f_tx(1, "JE-2026-0001", str(tx.id), [posting]),
        f_tx(2, "JE-2026-0001", str(tx.id), [posting]),
    )
    balances = balances_response(("1000", 10000), ("4000", -10000))
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert report.duplicate_rows == [
        {
            "kind": "duplicate",
            "transaction_id": str(tx.id),
            "reference": "JE-2026-0001",
            "txids": [1, 2],
            "indices": [0, 1],
        }
    ]
    assert report.leg_mismatches == []
    assert report.missing_in_formance == []
    assert "duplicate rows" in report.summary


async def test_duplicate_three_rows_all_txids_listed() -> None:
    """Three rows claiming one id -> all three txids in the entry."""
    bank = make_account("1000")
    revenue = make_account("4000")
    tx = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=10000), make_posting(revenue, credit=10000)],
    )
    db = FakeSession(transactions=[tx], accounts=[bank, revenue])

    posting = {"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}
    mirrors = list_response(
        f_tx(1, "JE-2026-0001", str(tx.id), [posting]),
        f_tx(2, "JE-2026-0001", str(tx.id), [posting]),
        f_tx(3, "JE-2026-0001", str(tx.id), [posting]),
    )
    balances = balances_response(("1000", 10000), ("4000", -10000))
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert len(report.duplicate_rows) == 1
    assert report.duplicate_rows[0]["txids"] == [1, 2, 3]
    assert report.duplicate_rows[0]["indices"] == [0, 1, 2]
    assert report.leg_mismatches == []


async def test_duplicate_rows_each_row_diffed() -> None:
    """Every claiming row is diffed — a wrong SECOND row is not invisible."""
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
            [{"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}],
        ),
        f_tx(
            2,
            "JE-2026-0001",
            str(tx.id),
            [{"source": "4000", "destination": "1000", "amount": "99.99", "asset": GBP}],
        ),
    )
    balances = balances_response(("1000", 10000), ("4000", -10000))
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert len(report.duplicate_rows) == 1
    assert len(report.leg_mismatches) == 1
    entry = report.leg_mismatches[0]
    assert entry["kind"] == "postings"
    assert entry["transaction_id"] == str(tx.id)
    assert entry["formance_txid"] == 2
    assert entry["actual"] == [
        {"source": "4000", "destination": "1000", "amount": "99.99", "asset": GBP}
    ]


async def test_duplicate_rows_both_wrong_two_entries() -> None:
    """Two wrong rows -> one leg_mismatches entry per row."""
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
        ),
        f_tx(
            2,
            "JE-2026-0001",
            str(tx.id),
            [{"source": "4000", "destination": "1000", "amount": "100.01", "asset": GBP}],
        ),
    )
    balances = balances_response(("1000", 10000), ("4000", -10000))
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert len(report.duplicate_rows) == 1
    assert [e["formance_txid"] for e in report.leg_mismatches] == [1, 2]
    assert [e["actual"][0]["amount"] for e in report.leg_mismatches] == [
        "99.99",
        "100.01",
    ]


async def test_duplicate_row_reference_mismatch_flagged() -> None:
    """A reference mismatch on one of several claiming rows is diffed."""
    bank = make_account("1000")
    revenue = make_account("4000")
    tx = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=10000), make_posting(revenue, credit=10000)],
    )
    db = FakeSession(transactions=[tx], accounts=[bank, revenue])

    posting = {"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}
    mirrors = list_response(
        f_tx(1, "JE-2026-0001", str(tx.id), [posting]),
        f_tx(2, "JE-2025-7777", str(tx.id), [posting]),
    )
    balances = balances_response(("1000", 10000), ("4000", -10000))
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert len(report.duplicate_rows) == 1
    assert report.leg_mismatches == [
        {
            "kind": "reference",
            "transaction_id": str(tx.id),
            "reference": "JE-2026-0001",
            "expected": "JE-2026-0001",
            "actual": "JE-2025-7777",
            "formance_txid": 2,
        }
    ]


# Gap 5: foreign ledger addresses


async def test_foreign_balance_address_flagged() -> None:
    """A balance address not resolvable to a COA code is foreign."""
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
            [{"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}],
        )
    )
    balances = {
        "data": [
            {"address": "1000", "balances": {GBP: "100.00"}},
            {"address": "4000", "balances": {GBP: "-100.00"}},
            {"address": "9999", "balances": {GBP: "1.00"}},
        ]
    }
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert report.foreign_addresses == [
        {"kind": "foreign_address", "address": "9999", "value": 100}
    ]
    assert report.balance_mismatches == []
    assert "foreign addresses" in report.summary


async def test_foreign_address_with_zero_value_still_flagged() -> None:
    """Even a zero-valued unknown address is foreign — it must not exist."""
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
            [{"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}],
        )
    )
    balances = {
        "data": [
            {"address": "1000", "balances": {GBP: "100.00"}},
            {"address": "4000", "balances": {GBP: "-100.00"}},
            {"address": "9999", "balances": {GBP: "0.00"}},
        ]
    }
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert report.foreign_addresses == [
        {"kind": "foreign_address", "address": "9999", "value": 0}
    ]


# Gap 6: sub-penny amounts


def test_decimal_to_pence_strict_rejects_sub_penny() -> None:
    with pytest.raises(InvalidOperation):
        _decimal_to_pence("1.005", strict=True)
    with pytest.raises(InvalidOperation):
        _decimal_to_pence("100.001", strict=True)
    with pytest.raises(InvalidOperation):
        _decimal_to_pence("garbage", strict=True)
    assert _decimal_to_pence("100.00", strict=True) == 10000
    assert _decimal_to_pence("0.60", strict=True) == 60
    assert _decimal_to_pence("-25.00", strict=True) == -2500
    assert _decimal_to_pence("0", strict=True) == 0


def test_decimal_to_pence_tolerant_truncation_unchanged() -> None:
    """F9's tolerant default is preserved: 1.005 still truncates to 100."""
    assert _decimal_to_pence("1.005") == 100
    assert _decimal_to_pence("1.005", strict=False) == 100
    assert _decimal_to_pence("100.00") == 10000


async def test_sub_penny_balance_is_parse_error() -> None:
    """A '1.005' balance is rejected in strict mode, never truncated."""
    bank = make_account("1000")
    revenue = make_account("4000")
    tx = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=100), make_posting(revenue, credit=100)],
    )
    db = FakeSession(transactions=[tx], accounts=[bank, revenue])

    mirrors = list_response(
        f_tx(
            1,
            "JE-2026-0001",
            str(tx.id),
            [{"source": "4000", "destination": "1000", "amount": "1.00", "asset": GBP}],
        )
    )
    balances = {
        "data": [
            {"address": "1000", "balances": {GBP: "1.005"}},
            {"address": "4000", "balances": {GBP: "-1.00"}},
        ]
    }
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert len(report.parse_errors) == 1
    entry = report.parse_errors[0]
    assert entry["kind"] == "parse_error"
    assert entry["address"] == "1000"
    assert entry["asset"] == GBP
    assert entry["raw"] == "1.005"
    assert "decimal places" in entry["error"]
    assert report.balance_mismatches == []
    assert "parse errors" in report.summary


async def test_sub_penny_balance_no_longer_matches_by_truncation() -> None:
    """The F2 false-ok: '1.005' truncating to 100 used to hide drift."""
    bank = make_account("1000")
    revenue = make_account("4000")
    tx = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=100), make_posting(revenue, credit=100)],
    )
    db = FakeSession(transactions=[tx], accounts=[bank, revenue])

    mirrors = list_response(
        f_tx(
            1,
            "JE-2026-0001",
            str(tx.id),
            [{"source": "4000", "destination": "1000", "amount": "1.00", "asset": GBP}],
        )
    )
    # Tolerant parsing would read "1.005" as 100 == expected 100 -> ok=True.
    balances = {
        "data": [
            {"address": "1000", "balances": {GBP: "1.005"}},
            {"address": "4000", "balances": {GBP: "-1.00"}},
        ]
    }
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert [e["raw"] for e in report.parse_errors] == ["1.005"]
    assert report.balance_mismatches == []
    assert report.missing_balance_addresses == []


# Gap 7: mixed assets


async def test_foreign_asset_balance_flagged() -> None:
    """A nonzero balance in a non-expected asset is a foreign_assets entry."""
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
            [{"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}],
        )
    )
    balances = {
        "data": [
            {"address": "1000", "balances": {GBP: "100.00", "USD/2": "50.00"}},
            {"address": "4000", "balances": {GBP: "-100.00"}},
        ]
    }
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is False
    assert report.foreign_assets == [
        {"kind": "foreign_asset", "address": "1000", "asset": "USD/2", "value": 5000}
    ]
    # The GBP balance is still compared exactly.
    assert report.balance_mismatches == []
    assert "foreign assets" in report.summary


async def test_zero_valued_foreign_asset_ignored() -> None:
    """A zero-valued non-expected asset is not a finding."""
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
            [{"source": "4000", "destination": "1000", "amount": "100.00", "asset": GBP}],
        )
    )
    balances = {
        "data": [
            {"address": "1000", "balances": {GBP: "100.00", "USD/2": "0.00"}},
            {"address": "4000", "balances": {GBP: "-100.00"}},
        ]
    }
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is True
    assert report.foreign_assets == []
    assert report.balance_mismatches == []


# Strict parse helper (function level)


def test_parse_balances_strict_returns_findings() -> None:
    balances, parse_errors, foreign_assets = _parse_balances(
        {
            "data": [
                {"address": "1000", "balances": {GBP: "1.00", "USD/2": "2.00"}},
                {"address": "4000", "balances": {GBP: "garbage"}},
            ]
        },
        strict=True,
    )
    assert balances == {"1000": 100}
    assert parse_errors == [
        {
            "kind": "parse_error",
            "address": "4000",
            "asset": GBP,
            "raw": "garbage",
            "error": parse_errors[0]["error"],
        }
    ]
    assert foreign_assets == [
        {"kind": "foreign_asset", "address": "1000", "asset": "USD/2", "value": 200}
    ]


def test_parse_balances_strict_dict_shape_payload() -> None:
    balances, parse_errors, foreign_assets = _parse_balances(
        {"data": {"1000": {GBP: "3.50"}, "4000": "-3.50"}}, strict=True
    )
    assert balances == {"1000": 350, "4000": -350}
    assert parse_errors == []
    assert foreign_assets == []


def test_parse_balances_tolerant_default_unchanged() -> None:
    """F9's tolerant default still sums every asset and degrades garbage."""
    assert _parse_balances(
        {
            "data": [
                {"address": "1000", "balances": {GBP: "1.00", "USD/2": "2.00"}},
                {"address": "4000", "balances": {GBP: "oops"}},
            ]
        }
    ) == {"1000": 300, "4000": 0}
    assert _parse_balances({"data": {"1000": "1.00"}}) == {"1000": 100}
    assert _parse_balances({"data": [{"address": "1000", "amount": "1.00"}]}) == {
        "1000": 100
    }
    assert _parse_balances("not-a-dict") == {}
    assert _parse_balances({"data": [{"address": None, "balances": {GBP: "1.00"}}]}) == {}


# Perfect ledger with all new lists empty


async def test_zero_net_ledger_clean_payload_reports_ok() -> None:
    """The ledger class where false-oks used to hide, now with a clean payload."""
    bank = make_account("1000")
    revenue = make_account("4000")
    tx1 = make_transaction(
        "JE-2026-0001",
        "posted",
        [make_posting(bank, debit=10000), make_posting(revenue, credit=10000)],
    )
    tx2 = make_transaction(
        "JE-2026-0002",
        "reversed",
        [make_posting(revenue, debit=10000), make_posting(bank, credit=10000)],
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
            [{"source": "1000", "destination": "4000", "amount": "100.00", "asset": GBP}],
        ),
    )
    balances = balances_response(("1000", 0), ("4000", 0))
    client = client_mock(mirrors, balances)

    report = await run_verify(db, client)

    assert report.ok is True
    assert report.missing_in_formance == []
    assert report.orphans_in_formance == []
    assert report.leg_mismatches == []
    assert report.balance_mismatches == []
    assert report.parse_errors == []
    assert report.duplicate_rows == []
    assert report.foreign_addresses == []
    assert report.foreign_assets == []
    assert report.missing_balance_addresses == []
    assert "OK" in report.summary
