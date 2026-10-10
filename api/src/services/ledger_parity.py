"""Ledger parity verifier — proves Formance mirrors Postgres faithfully (Contract F2).

Compares the set of posted/reversed Postgres transactions (and the paired
Formance postings ``build_formance_postings`` produces for them) against the
Formance ledger, plus per-account balances against ``get_balances``.  Every
discrepancy is reported precisely with expected vs actual values.  Formance
being unreachable yields a report with ``ok=False`` and a non-empty ``error``
field — it never raises.

Contract F13 hardening (the 7 F2 verifier gaps):

1. Malformed balance values are ``parse_errors`` (never silent zeros) and a
   mirror address absent from ``get_balances`` is a
   ``missing_balance_addresses`` finding, not an assumed 0.
2. The balance universe is every COA account with any in-scope posting
   (posted+reversed), regardless of ``is_active``, plus every COA account
   Formance holds a nonzero balance for.
3./4. Formance rows are grouped by ``metadata.transaction_id``; more than one
   row claiming an id yields a ``duplicate_rows`` entry (both txids listed)
   and EVERY candidate row is diffed — each mismatch gets its own entry.
5. A Formance balance address not resolvable to a COA code is a
   ``foreign_addresses`` entry with its value.
6. Strict parsing rejects balance strings with more than 2 decimal places as
   ``parse_errors`` instead of truncating (1.005 no longer reads as 100).
7. Balances are parsed per asset; the mirror asset ``<currency>/2`` (F1's
   convention, default ``GBP/2``) is compared and any other asset holding a
   nonzero value is a ``foreign_assets`` entry.

Strict mode is opt-in inside :func:`verify_parity`: the default behavior of
:func:`_parse_balances` / :func:`_decimal_to_pence` is unchanged (F9's report
engine and every other tolerant caller keep working).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, Overflow
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

# F1's mirror asset for a transaction is "<currency>/2" (see
# build_formance_postings in transaction_service).  Parity expects, per
# address, the balance held in exactly those assets; any other nonzero
# asset is a foreign_assets finding.  GBP/2 is the default when a
# transaction carries no currency.
DEFAULT_EXPECTED_ASSET = "GBP/2"


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
                             "actual": <postings list or reference str>,
                             "formance_txid": <row txid, duplicate groups only>}
      balance_mismatches:   {"kind": "balance", "account_id", "account_code",
                             "expected": <pence int>, "actual": <pence int>}
      parse_errors:         {"kind": "parse_error", "address", "asset", "raw", "error"}
      duplicate_rows:       {"kind": "duplicate", "transaction_id", "reference",
                             "txids": [<every claiming Formance txid>],
                             "indices": [<position of each row in the group>]}
      foreign_addresses:    {"kind": "foreign_address", "address", "value"}
      foreign_assets:       {"kind": "foreign_asset", "address", "asset", "value"}
      missing_balance_addresses: {"kind": "missing_balance", "address"}
    """

    ok: bool
    missing_in_formance: list[dict[str, Any]] = field(default_factory=list)
    orphans_in_formance: list[dict[str, Any]] = field(default_factory=list)
    leg_mismatches: list[dict[str, Any]] = field(default_factory=list)
    balance_mismatches: list[dict[str, Any]] = field(default_factory=list)
    parse_errors: list[dict[str, Any]] = field(default_factory=list)
    duplicate_rows: list[dict[str, Any]] = field(default_factory=list)
    foreign_addresses: list[dict[str, Any]] = field(default_factory=list)
    foreign_assets: list[dict[str, Any]] = field(default_factory=list)
    missing_balance_addresses: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Amount / posting normalisation (exact Decimal arithmetic, never float)
# ---------------------------------------------------------------------------


def _decimal_to_pence(raw: Any, strict: bool = False) -> int:
    """Exact Decimal round-trip: Formance amount string -> integer pence.

    Default (tolerant, F9's behavior) truncates toward zero for values with
    more than 2 decimal places.  ``strict=True`` rejects any value with more
    than 2 decimal places (or a non-finite value) with ``InvalidOperation``
    instead of silently truncating — 1.005 is a parse error, never 100.
    """
    value = Decimal(str(raw))
    if strict and (not value.is_finite() or value.as_tuple().exponent < -2):
        raise InvalidOperation(
            f"amount {str(raw)!r} has more than 2 decimal places"
        )
    return int(value * 100)


def _safe_pence(raw: Any) -> int:
    """Decimal round-trip that degrades to 0 for unparseable amounts."""
    try:
        return _decimal_to_pence(raw)
    except (InvalidOperation, Overflow, TypeError, ValueError):
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


def _parse_balances_strict(
    raw: Any, expected_assets: set[str]
) -> tuple[dict[str, int], list[dict[str, Any]], list[dict[str, Any]]]:
    """Strict per-asset parse of ``get_balances`` (Contract F13).

    Returns ``(balances, parse_errors, foreign_assets)``:

    - ``balances``: ``address -> pence`` summed over the expected assets
      only; an address holding a zero expected-asset total is still present.
    - ``parse_errors``: every balance value that cannot be parsed exactly
      (garbage or more than 2 decimal places), raw string preserved.
    - ``foreign_assets``: every nonzero balance held in an asset outside
      the expected set.
    """
    if not isinstance(raw, dict):
        return {}, [
            {
                "kind": "parse_error",
                "address": None,
                "asset": None,
                "raw": repr(raw)[:200],
                "error": "malformed balances payload (expected a dict envelope)",
            }
        ], []
    data = raw.get("data", raw)
    if isinstance(data, dict):
        data = data.get("balances", data)

    balances: dict[str, int] = {}
    parse_errors: list[dict[str, Any]] = []
    foreign_assets: list[dict[str, Any]] = []

    def record(address: str, asset: str, value: Any) -> None:
        try:
            pence = _decimal_to_pence(value, strict=True)
        except (InvalidOperation, Overflow, TypeError, ValueError) as exc:
            parse_errors.append(
                {
                    "kind": "parse_error",
                    "address": address,
                    "asset": asset,
                    "raw": str(value),
                    "error": str(exc),
                }
            )
            return
        if asset and asset not in expected_assets:
            if pence != 0:
                foreign_assets.append(
                    {
                        "kind": "foreign_asset",
                        "address": address,
                        "asset": asset,
                        "value": pence,
                    }
                )
            return
        balances[address] = balances.get(address, 0) + pence

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            address = item.get("address")
            if address is None:
                continue
            key = str(address)
            if isinstance(item.get("balances"), dict):
                for asset, value in item["balances"].items():
                    record(key, str(asset), value)
            elif "amount" in item:
                record(key, "", item["amount"])
    elif isinstance(data, dict):
        for address, value in data.items():
            key = str(address)
            if isinstance(value, dict):
                for asset, sub in value.items():
                    record(key, str(asset), sub)
            else:
                record(key, "", value)
    return balances, parse_errors, foreign_assets


def _parse_balances(
    raw: Any,
    strict: bool = False,
    expected_assets: Optional[set[str]] = None,
) -> dict[str, int] | tuple[dict[str, int], list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract ``address -> total pence`` from ``get_balances``.

    Default (tolerant — F9's report engine and every existing caller): sums
    every asset per address with :func:`_safe_pence`, garbage degrading to 0
    and sub-penny values truncating; the behavior is unchanged.

    ``strict=True`` switches to the F13 strict parse: only the expected
    assets contribute to the returned balances and the caller additionally
    receives the ``parse_errors`` and ``foreign_assets`` findings as
    ``(balances, parse_errors, foreign_assets)``.
    """
    if strict:
        return _parse_balances_strict(raw, expected_assets or {DEFAULT_EXPECTED_ASSET})
    if not isinstance(raw, dict):
        return {}
    data = raw.get("data", raw)
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
                for raw_value in item["balances"].values():
                    out[key] = out.get(key, 0) + _safe_pence(raw_value)
            elif "amount" in item:
                out[key] = out.get(key, 0) + _safe_pence(item["amount"])
    elif isinstance(data, dict):
        for address, value in data.items():
            key = str(address)
            if isinstance(value, dict):
                for raw_value in value.values():
                    out[key] = out.get(key, 0) + _safe_pence(raw_value)
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
    parse_errors: list[dict[str, Any]] = []
    duplicate_rows: list[dict[str, Any]] = []
    foreign_addresses: list[dict[str, Any]] = []
    foreign_assets: list[dict[str, Any]] = []
    missing_balance_addresses: list[dict[str, Any]] = []

    # ---- Postgres reads -------------------------------------------------
    tx_result = await db.execute(
        select(Transaction)
        .where(Transaction.status.in_(IN_SCOPE_STATUSES))
        .options(selectinload(Transaction.postings))
    )
    pg_transactions: list[Transaction] = list(tx_result.scalars().all())

    account_result = await db.execute(select(Account))
    accounts: list[Account] = list(account_result.scalars().all())
    codes = {a.id: a.code for a in accounts}
    code_to_account = {str(a.code): a for a in accounts}

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

    # Expected assets: F1 mirrors with "<currency>/2"; GBP/2 is the default
    # when a transaction carries no currency.
    expected_assets = {DEFAULT_EXPECTED_ASSET}
    for tx in pg_transactions:
        expected_assets.add(f"{tx.currency or 'GBP'}/2")

    # Addresses the in-scope mirror postings touch — each must be present in
    # get_balances (Contract F13 gap 1).
    mirror_addresses: set[str] = set()

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

        if len(candidates) > 1:
            duplicate_rows.append(
                {
                    "kind": "duplicate",
                    "transaction_id": tid,
                    "reference": tx.reference,
                    "txids": [c.get("txid") for c in candidates],
                    "indices": list(range(len(candidates))),
                }
            )

        expected_postings = build_formance_postings(
            tx.postings, currency=tx.currency or "GBP", codes=codes
        )
        expected_ref = tx.reference or tid
        expected_multiset = _postings_multiset(expected_postings)

        # Every candidate row is diffed; each mismatch gets its own entry.
        for ftx in candidates:
            for posting in ftx.get("postings") or []:
                if isinstance(posting, dict):
                    for key in ("source", "destination"):
                        value = posting.get(key)
                        if value:
                            mirror_addresses.add(str(value))
            actual_postings = ftx.get("postings") or []
            if _postings_multiset(actual_postings) != expected_multiset:
                entry = {
                    "kind": "postings",
                    "transaction_id": tid,
                    "reference": tx.reference,
                    "expected": expected_postings,
                    "actual": actual_postings,
                }
                if len(candidates) > 1:
                    entry["formance_txid"] = ftx.get("txid")
                leg_mismatches.append(entry)
            if ftx.get("reference") != expected_ref:
                entry = {
                    "kind": "reference",
                    "transaction_id": tid,
                    "reference": tx.reference,
                    "expected": expected_ref,
                    "actual": ftx.get("reference"),
                }
                if len(candidates) > 1:
                    entry["formance_txid"] = ftx.get("txid")
                leg_mismatches.append(entry)

    # ---- Balance-level parity -------------------------------------------
    expected_balance: dict[Any, int] = {}
    for tx in pg_transactions:
        for posting in tx.postings:
            expected_balance[posting.account_id] = expected_balance.get(
                posting.account_id, 0
            ) + (posting.debit_amount - posting.credit_amount)

    formance_balances, parse_errors, foreign_assets = _parse_balances(
        balances_response, strict=True, expected_assets=expected_assets
    )

    seen_addresses = set(formance_balances)
    seen_addresses.update(f["address"] for f in foreign_assets)
    seen_addresses.update(e["address"] for e in parse_errors)

    # Gap 1: an address the in-scope mirror postings touch but get_balances
    # omits is reported, never assumed to be 0.
    for address in sorted(mirror_addresses - seen_addresses):
        missing_balance_addresses.append(
            {"kind": "missing_balance", "address": address}
        )

    def total_for(address: str) -> int:
        return formance_balances.get(address, 0) + sum(
            f["value"] for f in foreign_assets if f["address"] == address
        )

    # Gap 5: every balance address not resolvable to a COA code is foreign.
    for address in sorted(seen_addresses):
        if address not in code_to_account:
            foreign_addresses.append(
                {
                    "kind": "foreign_address",
                    "address": address,
                    "value": total_for(address),
                }
            )

    # Gap 2: the balance universe is every COA account with an in-scope
    # posting (posted+reversed), regardless of is_active, plus every COA
    # account Formance holds a nonzero balance for.
    universe: list[Account] = [a for a in accounts if a.id in expected_balance]
    universe_ids = {a.id for a in universe}
    for address in sorted(seen_addresses):
        account = code_to_account.get(address)
        if account is None or account.id in universe_ids:
            continue
        if total_for(address) != 0:
            universe.append(account)
            universe_ids.add(account.id)

    # Addresses whose value is missing or unparseable are reported by their
    # own finding — never compared as an assumed 0.
    unfixed = {e["address"] for e in missing_balance_addresses}
    unfixed.update(e["address"] for e in parse_errors)

    for account in universe:
        if account.code in unfixed:
            continue
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
    ok = not (
        missing
        or orphans
        or leg_mismatches
        or balance_mismatches
        or parse_errors
        or duplicate_rows
        or foreign_addresses
        or foreign_assets
        or missing_balance_addresses
    )
    if ok:
        summary = (
            f"Ledger parity OK: {len(pg_transactions)} transactions and "
            f"{len(universe)} accounts verified"
        )
    else:
        findings = [
            (len(missing), "missing"),
            (len(orphans), "orphans"),
            (len(leg_mismatches), "leg mismatches"),
            (len(balance_mismatches), "balance mismatches"),
            (len(parse_errors), "parse errors"),
            (len(duplicate_rows), "duplicate rows"),
            (len(foreign_addresses), "foreign addresses"),
            (len(foreign_assets), "foreign assets"),
            (len(missing_balance_addresses), "missing balance addresses"),
        ]
        summary = "Ledger parity FAILED: " + ", ".join(
            f"{count} {label}" for count, label in findings if count
        )
    return ParityReport(
        ok=ok,
        missing_in_formance=missing,
        orphans_in_formance=orphans,
        leg_mismatches=leg_mismatches,
        balance_mismatches=balance_mismatches,
        parse_errors=parse_errors,
        duplicate_rows=duplicate_rows,
        foreign_addresses=foreign_addresses,
        foreign_assets=foreign_assets,
        missing_balance_addresses=missing_balance_addresses,
        summary=summary,
    )
