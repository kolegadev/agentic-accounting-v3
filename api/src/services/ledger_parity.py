"""Ledger parity verifier — proves Formance mirrors Postgres faithfully (Contract F2).

Compares the set of posted/reversed Postgres transactions (and the paired
Formance postings ``build_formance_postings`` produces for them) against the
Formance ledger, plus per-account balances against ``get_balances``.  Every
discrepancy is reported precisely with expected vs actual values.  Formance
being unreachable yields a report with ``ok=False`` and a non-empty ``error``
field — it never raises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.account import Account
from src.models.transaction import Transaction
from src.services.formance_client import formance_client
from src.services.transaction_service import build_formance_postings

IN_SCOPE_STATUSES = ("posted", "reversed")
PAGE_SIZE = 500
MAX_PAGES = 1000


@dataclass
class ParityReport:
    """Outcome of a single parity verification pass.

    Entry shapes:
      missing_in_formance:  {"kind": "missing_mirror", "transaction_id", "reference",
                             "expected": <pg transaction id str>, "actual": None}
      orphans_in_formance:  {"kind": "orphan", "transaction_id": <claimed metadata id or None>,
                             "reference", "formance_txid", "expected": None,
                             "actual": {"txid", "reference"}}
      leg_mismatches:       {"kind": "postings" | "reference", "transaction_id", "reference",
                             "expected": <postings list or reference str>,
                             "actual": <postings list or reference str>}
      balance_mismatches:   {"kind": "balance", "account_id", "account_code",
                             "expected": <pence int>, "actual": <pence int>}
    """

    ok: bool
    missing_in_formance: list[dict[str, Any]] = field(default_factory=list)
    orphans_in_formance: list[dict[str, Any]] = field(default_factory=list)
    leg_mismatches: list[dict[str, Any]] = field(default_factory=list)
    balance_mismatches: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Amount / posting normalisation (exact Decimal arithmetic, never float)
# ---------------------------------------------------------------------------


def _decimal_to_pence(raw: Any) -> int:
    """Exact Decimal round-trip: Formance amount string -> integer pence."""
    return int(Decimal(str(raw)) * 100)


def _safe_pence(raw: Any) -> int:
    """Decimal round-trip that degrades to 0 for unparseable amounts."""
    try:
        return _decimal_to_pence(raw)
    except (InvalidOperation, TypeError, ValueError):
        return 0


def _normalise_posting(posting: dict[str, Any]) -> tuple[str, str, Optional[Decimal], str]:
    source = str(posting.get("source") or "")
    destination = str(posting.get("destination") or "")
    asset = str(posting.get("asset") or "")
    raw = posting.get("amount")
    try:
        amount: Optional[Decimal] = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        amount = None
    return (source, destination, amount, asset)


def _postings_multiset(
    postings: list[dict[str, Any]],
) -> list[tuple[str, str, Optional[Decimal], str]]:
    """Order-insensitive normalisation of a posting list for equality checks."""
    normalised = [_normalise_posting(p) for p in postings]
    return sorted(normalised, key=lambda t: (t[0], t[1], str(t[2]), t[3]))


def _parse_balances(response: Any) -> dict[str, int]:
    """Extract ``address -> total pence`` (summed across assets) from get_balances."""
    if not isinstance(response, dict):
        return {}
    data = response.get("data", response)
    if isinstance(data, dict):
        data = data.get("balances", data)

    out: dict[str, int] = {}
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            address = item.get("address")
            if address is None:
                continue
            key = str(address)
            if isinstance(item.get("balances"), dict):
                for raw in item["balances"].values():
                    out[key] = out.get(key, 0) + _safe_pence(raw)
            elif "amount" in item:
                out[key] = out.get(key, 0) + _safe_pence(item["amount"])
    elif isinstance(data, dict):
        for address, value in data.items():
            key = str(address)
            if isinstance(value, dict):
                for raw in value.values():
                    out[key] = out.get(key, 0) + _safe_pence(raw)
            else:
                out[key] = out.get(key, 0) + _safe_pence(value)
    return out


# ---------------------------------------------------------------------------
# Formance reads
# ---------------------------------------------------------------------------


async def _list_all_formance_transactions(ledger: str) -> list[dict[str, Any]]:
    """Fetch every Formance transaction for a ledger, following the cursor."""
    collected: list[dict[str, Any]] = []
    after: Optional[str] = None
    for _ in range(MAX_PAGES):
        response = await formance_client.list_transactions(
            ledger, page_size=PAGE_SIZE, after=after
        )
        if not isinstance(response, dict):
            break
        cursor = response.get("cursor") or {}
        batch = response.get("data") or []
        if isinstance(batch, list):
            collected.extend(batch)
        if not cursor.get("hasMore"):
            break
        nxt: Optional[str] = None
        if cursor.get("next"):
            nxt = str(cursor["next"])
        elif batch and isinstance(batch[-1], dict):
            nxt = str(batch[-1].get("txid"))
        if not nxt or nxt == after:
            break
        after = nxt
    return collected


# ---------------------------------------------------------------------------
# The verifier
# ---------------------------------------------------------------------------


async def verify_parity(db: AsyncSession, ledger: str = "main") -> ParityReport:
    """Verify Formance ledger ``ledger`` is a faithful image of Postgres.

    Reads in-scope transactions (posted/reversed) and accounts from ``db``;
    reads the Formance transaction list and balances through the
    ``formance_client`` singleton.  Formance being unreachable produces a
    report with ``ok=False`` and a non-empty ``error`` (never raises).
    """
    missing: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = []
    leg_mismatches: list[dict[str, Any]] = []
    balance_mismatches: list[dict[str, Any]] = []

    # ---- Postgres reads -------------------------------------------------
    tx_result = await db.execute(
        select(Transaction)
        .where(Transaction.status.in_(IN_SCOPE_STATUSES))
        .options(selectinload(Transaction.postings))
    )
    pg_transactions: list[Transaction] = list(tx_result.scalars().all())

    account_result = await db.execute(select(Account))
    accounts: list[Account] = list(account_result.scalars().all())
    active_accounts = [a for a in accounts if a.is_active]
    codes = {a.id: a.code for a in accounts}

    # ---- Formance reads (never raise) -----------------------------------
    try:
        formance_transactions = await _list_all_formance_transactions(ledger)
        balances_response = await formance_client.get_balances(ledger)
    except Exception as exc:  # noqa: BLE001 — parity must degrade, not crash
        message = f"{exc.__class__.__name__}: {exc}"
        return ParityReport(
            ok=False,
            missing_in_formance=missing,
            orphans_in_formance=orphans,
            leg_mismatches=leg_mismatches,
            balance_mismatches=balance_mismatches,
            summary=f"Ledger parity FAILED: Formance ledger '{ledger}' unreachable: {message}",
            error=message,
        )

    # ---- Transaction-level parity (Postgres -> Formance) ----------------
    in_scope_ids = {str(t.id) for t in pg_transactions}
    formance_by_txid: dict[str, list[dict[str, Any]]] = {}
    for ftx in formance_transactions:
        if not isinstance(ftx, dict):
            orphans.append(
                {
                    "kind": "orphan",
                    "transaction_id": None,
                    "reference": None,
                    "formance_txid": None,
                    "expected": None,
                    "actual": ftx,
                }
            )
            continue
        meta = ftx.get("metadata")
        txid_meta = meta.get("transaction_id") if isinstance(meta, dict) else None
        if not txid_meta or str(txid_meta) not in in_scope_ids:
            orphans.append(
                {
                    "kind": "orphan",
                    "transaction_id": str(txid_meta) if txid_meta else None,
                    "reference": ftx.get("reference"),
                    "formance_txid": ftx.get("txid"),
                    "expected": None,
                    "actual": {
                        "txid": ftx.get("txid"),
                        "reference": ftx.get("reference"),
                    },
                }
            )
            continue
        formance_by_txid.setdefault(str(txid_meta), []).append(ftx)

    for tx in pg_transactions:
        tid = str(tx.id)
        candidates = formance_by_txid.get(tid, [])
        if not candidates:
            missing.append(
                {
                    "kind": "missing_mirror",
                    "transaction_id": tid,
                    "reference": tx.reference,
                    "expected": tid,
                    "actual": None,
                }
            )
            continue

        expected_postings = build_formance_postings(
            tx.postings, currency=tx.currency or "GBP", codes=codes
        )
        expected_ref = tx.reference or tid
        matched = any(
            ftx.get("reference") == expected_ref
            and _postings_multiset(ftx.get("postings") or [])
            == _postings_multiset(expected_postings)
            for ftx in candidates
        )
        if matched:
            continue

        ftx = candidates[0]
        actual_postings = ftx.get("postings") or []
        if _postings_multiset(actual_postings) != _postings_multiset(expected_postings):
            leg_mismatches.append(
                {
                    "kind": "postings",
                    "transaction_id": tid,
                    "reference": tx.reference,
                    "expected": expected_postings,
                    "actual": actual_postings,
                }
            )
        if ftx.get("reference") != expected_ref:
            leg_mismatches.append(
                {
                    "kind": "reference",
                    "transaction_id": tid,
                    "reference": tx.reference,
                    "expected": expected_ref,
                    "actual": ftx.get("reference"),
                }
            )

    # ---- Balance-level parity -------------------------------------------
    expected_balance: dict[Any, int] = {}
    for tx in pg_transactions:
        for posting in tx.postings:
            expected_balance[posting.account_id] = expected_balance.get(
                posting.account_id, 0
            ) + (posting.debit_amount - posting.credit_amount)

    formance_balances = _parse_balances(balances_response)
    for account in active_accounts:
        expected = expected_balance.get(account.id, 0)
        actual = formance_balances.get(account.code, 0)
        if actual != expected:
            balance_mismatches.append(
                {
                    "kind": "balance",
                    "account_id": str(account.id),
                    "account_code": account.code,
                    "expected": expected,
                    "actual": actual,
                }
            )

    # ---- Report ----------------------------------------------------------
    ok = not (missing or orphans or leg_mismatches or balance_mismatches)
    if ok:
        summary = (
            f"Ledger parity OK: {len(pg_transactions)} transactions and "
            f"{len(active_accounts)} active accounts verified"
        )
    else:
        summary = (
            f"Ledger parity FAILED: {len(missing)} missing, {len(orphans)} orphans, "
            f"{len(leg_mismatches)} leg mismatches, "
            f"{len(balance_mismatches)} balance mismatches"
        )
    return ParityReport(
        ok=ok,
        missing_in_formance=missing,
        orphans_in_formance=orphans,
        leg_mismatches=leg_mismatches,
        balance_mismatches=balance_mismatches,
        summary=summary,
    )
