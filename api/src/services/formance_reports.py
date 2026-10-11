"""Contract F9 — Formance-derived report engine (P&L, BS, TB, cash flow, funds).

Computes all five F8 reports with amounts from the Formance ledger and
classification from the Postgres COA. Every report enforces its identity in
code and raises :class:`ReportEngineError` on identity violations or
classification gaps (unknown account codes) — a broken report is never
returned silently.

Data sources (exact F8/F2 conventions):

- Balance-based reports (BS, TB) read ``formance_client.get_balances``: the
  address balance equals debits − credits in integer pence. Credit-nature
  accounts therefore carry NEGATIVE Formance balances and the recipes negate
  them for presentation (BS liabilities/equity positive; TB two-column by
  sign).
- Flow-based reports (P&L, CF, funds) read ``formance_client.list_transactions``
  following the cursor (page_size 500, same pagination as the F2 parity
  verifier), keep only transactions whose ``metadata.transaction_id`` belongs
  to a Postgres transaction with status ``posted`` or ``reversed`` (the F2
  scope rule), filter the period Python-side on the mirror ``timestamp``
  (mirrors carry the transaction's effective_date; missing/unparseable timestamps are treated as in-scope), and accumulate
  per-code flows: ``destination`` debited (+amount), ``source`` credited.
  Working-capital movements and the change-in-bank are period NET FLOWS of
  the relevant accounts — a period net flow IS the balance change, so no
  historical-balance query is needed.

Money stays exact: every Formance decimal string converts via
``Decimal(str(amount)) × 100`` (the parity verifier's ``_decimal_to_pence``).
No float anywhere on the money path.

Cross-checking: :func:`verify_against_postgres` compares P&L/BS/TB against
the existing Postgres :class:`~src.services.report_service.ReportService`
outputs (they must agree exactly when ledger parity holds). Cash flow and
funds have no Postgres-side equivalent and return an empty diff.

Known conventions (documented, deterministic):

- Formance ``get_balances`` has no as-of-date: BS/TB use current balances;
  the caller must ensure the ledger holds data as at ``as_at``.
- The F8 corporation-tax recipe: the charge is the GROSS credit flow on
  2040 (= closing − opening + tax paid). The provision entry MUST pair
  Dr 3020 (equity) / Cr 2040; debiting an expense account for the
  provision double-counts the charge in the P&L and breaks the CF
  identity (the engine raises loudly rather than fudge).
- BS presents VAT as one signed net liability line (F26 — matches the
  Postgres ReportService exactly, so the cross-check is meaningful)
  presentation rule); this reclassification intentionally differs from the
  Postgres ReportService, which never reclassifies.

Contract F24: the balance-sheet equity section gains a flow-derived
"Profit for the period" line (the P&L net over ``[period_start or
date.min, as_at]``, computed with the SAME flow machinery as
``generate_pl``) and the identity becomes
assets == liabilities + equity + current_profit — a ledger with posted
income/expenses no longer needs closing entries to balance.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timezone
from decimal import InvalidOperation
from typing import Any, Optional
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.account import Account
from src.models.transaction import Posting, Transaction
from src.services.formance_client import formance_client
from src.services.ledger_parity import (
    IN_SCOPE_STATUSES,
    _decimal_to_pence,
    _parse_balances,
)

__all__ = [
    "ReportEngineError",
    "LINE_ITEM_CODES",
    "LINE_ITEM_CLASSES",
    "build_code_class_map",
    "generate_pl",
    "generate_bs",
    "generate_tb",
    "generate_cash_flow",
    "generate_funds",
    "generate_ar_aging",
    "generate_ap_aging",
    "verify_against_postgres",
]

DEFAULT_LEDGER = "main"
PAGE_SIZE = 500
MAX_PAGES = 1000


class ReportEngineError(Exception):
    """A report could not be produced: identity violation or classification gap."""


# ---------------------------------------------------------------------------
# Line-item code lists — the F8 catalog is the spec (bound statically in
# ``tests/unit/test_formance_reports.py`` by parsing the SKILL.md
# Construction rules). Keyed by skill id; the runtime resolves the code
# groups below FROM these tables so the binding test always covers what the
# engine actually uses.
# ---------------------------------------------------------------------------

LINE_ITEM_CODES: dict[str, dict[str, tuple[str, ...]]] = {
    "profit-and-loss": {
        "revenue": ("4000", "4010", "4020", "4030", "4050", "4060"),
        "direct_costs": ("5000", "5010", "5020", "5100", "5110"),
        "overheads": (
            "6000", "6010", "6020", "6030", "6040", "6050",
            "6100", "6110", "6200", "6210", "6220", "6300", "6310", "6400",
            "6510", "6700", "6990",
        ),
        "staff_costs": ("6600", "6610", "6620", "6630"),
        "depreciation": ("6500",),
        "finance_costs": ("6410",),
        "corporation_tax": ("2040",),
    },
    "balance-sheet": {
        "bank": ("1000", "1010"),
        "current_assets": ("1100", "1110", "1120", "1130", "1200", "1210"),
        "fixed_assets": ("1300", "1310", "1320", "1330", "1340", "1350"),
        "accumulated_depreciation": ("1360",),
        "current_liabilities": ("2000", "2010", "2020", "2030", "2040"),
        "vat": ("2200", "2210", "2220", "2230"),
        "long_term_liabilities": ("2100", "2110"),
        "equity": ("3000", "3010", "3020"),
        "contra_equity": ("3030",),
    },
    "cash-flow-statement": {
        "depreciation_addback": ("6500",),
        "working_capital_assets": ("1100", "1110"),
        "other_current_assets": ("1120", "1130", "1200", "1210"),
        "working_capital_liabilities": ("2000", "2010", "2020", "2030", "2040"),
        "vat": ("2200", "2210", "2220", "2230"),
        "fixed_asset_purchases": ("1300", "1310", "1320", "1330", "1340", "1350"),
        "borrowings": ("2100", "2110"),
        "capital_introduced": ("3000",),
        "dividends_drawings": ("3010", "3030"),
        "bank": ("1000", "1010"),
    },
    "funds-statement": {
        "depreciation": ("6500",),
        "borrowings": ("2100", "2110"),
        "capital_introduced": ("3000",),
        "fixed_asset_purchases": ("1300", "1310", "1320", "1330", "1340", "1350"),
        "loan_repayments": ("2100", "2110"),
        "dividends_drawings": ("3010", "3030"),
        "working_capital_assets": (
            "1000", "1010", "1100", "1110", "1120", "1130", "1200", "1210",
        ),
        "working_capital_liabilities": (
            "2000", "2010", "2020", "2030", "2040",
            "2200", "2210", "2220", "2230",
        ),
    },
    "trial-balance": {},
}

LINE_ITEM_CLASSES: dict[str, dict[str, tuple[str, ...]]] = {
    "profit-and-loss": {
        "revenue": ("Revenue (Revenue)",),
        "direct_costs": ("Expense (DirectCost)",),
        "expenses": ("Expense (Expense)",),
    },
    "balance-sheet": {
        "assets": ("Asset (Bank)", "Asset (CurrentAsset)", "Asset (FixedAsset)"),
        "liabilities": (
            "Liability (CurrentLiability)",
            "Liability (LongTermLiability)",
        ),
        "equity": ("Equity (Equity)",),
    },
    "trial-balance": {
        "debit": (
            "Asset (Bank)",
            "Asset (CurrentAsset)",
            "Asset (FixedAsset)",
            "Expense (Expense)",
            "Expense (DirectCost)",
        ),
        "credit": (
            "Liability (CurrentLiability)",
            "Liability (LongTermLiability)",
            "Equity (Equity)",
            "Revenue (Revenue)",
        ),
    },
}

# Runtime code groups (single source of truth: the binding tables above).
_REVENUE_CODES = LINE_ITEM_CODES["profit-and-loss"]["revenue"]
_DIRECT_CODES = LINE_ITEM_CODES["profit-and-loss"]["direct_costs"]
_OVERHEAD_CODES = LINE_ITEM_CODES["profit-and-loss"]["overheads"]
_RAW_PL_GROUPS = (
    "revenue",
    "direct_costs",
    "overheads",
    "staff_costs",
    "depreciation",
    "finance_costs",
    "corporation_tax",
)
_VAT_CODES = LINE_ITEM_CODES["balance-sheet"]["vat"]
_DEPRECIATION_CODES = LINE_ITEM_CODES["profit-and-loss"]["depreciation"]
_STAFF_CODES = LINE_ITEM_CODES["profit-and-loss"]["staff_costs"]
_FINANCE_CODES = LINE_ITEM_CODES["profit-and-loss"]["finance_costs"]
_TAX_CODES = LINE_ITEM_CODES["profit-and-loss"]["corporation_tax"]
_CONTRA_DEPRECIATION_CODES = LINE_ITEM_CODES["balance-sheet"][
    "accumulated_depreciation"
]
_CONTRA_EQUITY_CODES = LINE_ITEM_CODES["balance-sheet"]["contra_equity"]
_WC_ASSET_CODES = LINE_ITEM_CODES["cash-flow-statement"]["working_capital_assets"]
_DIVIDEND_CODES = LINE_ITEM_CODES["cash-flow-statement"]["dividends_drawings"]

# Contract F37 — ledger-derived AR/AP aging: report name -> (account code,
# COA label fallback); buckets are always reported in this order.
AGING_BUCKETS = ("0-30", "31-60", "61-90", "90+")
_AGING_DEFS = {
    "aged_receivables": ("1100", "Trade Debtors"),
    "aged_payables": ("2000", "Trade Creditors"),
}
_AGING_NOTES = {
    "aged_receivables": (
        "Ledger-derived aging: invoice moments (debits to 1100) FIFO-"
        "matched against customer receipts (credits to 1100) from the same "
        "posted ledger the Balance Sheet reads."
    ),
    "aged_payables": (
        "Ledger-derived aging: supplier-invoice moments (credits to 2000) "
        "FIFO-matched against supplier payments (debits to 2000) from the "
        "same posted ledger the Balance Sheet reads."
    ),
}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _account_attr(account: Any, name: str) -> Any:
    """Read ``name`` from an ORM account object or a plain dict."""
    try:
        return getattr(account, name)
    except AttributeError:
        return account[name]


def build_code_class_map(coa_accounts: Any) -> dict[str, str]:
    """Map every COA account CODE to its canonical ``"Category (Type)"`` class.

    Accepts ORM ``Account`` objects (attributes) or plain dicts with
    ``code`` / ``category`` / ``type`` keys — the same shape
    ``src.constructors.resolve_from_coa`` builds for the entry constructors.
    """
    return {
        str(_account_attr(a, "code")): f"{_account_attr(a, 'category')} "
        f"({_account_attr(a, 'type')})"
        for a in coa_accounts
    }


# ---------------------------------------------------------------------------
# Formance reads
# ---------------------------------------------------------------------------


def _parse_timestamp(raw: Any) -> Optional[datetime]:
    """ISO 8601 mirror timestamp -> aware UTC datetime (None when missing)."""
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _period_window(
    period_start: Optional[date], period_end: Optional[date]
) -> tuple[Optional[datetime], Optional[datetime]]:
    """Inclusive [start-of-day, end-of-day] UTC window for the period."""
    start = (
        datetime.combine(period_start, time.min, tzinfo=timezone.utc)
        if period_start
        else None
    )
    end = (
        datetime.combine(period_end, time(23, 59, 59, 999999), tzinfo=timezone.utc)
        if period_end
        else None
    )
    return start, end


async def _list_all_transactions(ledger: str) -> list[dict[str, Any]]:
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
        nxt = cursor.get("next")
        if not nxt and batch and isinstance(batch[-1], dict):
            nxt = batch[-1].get("txid")
        if not nxt or str(nxt) == str(after):
            break
        after = str(nxt)
    return collected


# ---------------------------------------------------------------------------
# Postgres reads
# ---------------------------------------------------------------------------


async def _load_coa(
    db: AsyncSession,
    *,
    entity_id: Optional[uuid.UUID] = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """(code -> class, code -> name) for every COA account.

    ``entity_id`` (Contract F19) scopes the COA classification to one
    entity's accounts — two COAs sharing codes never collapse into one
    map; ``None`` keeps the legacy global classification.
    """
    stmt = select(Account).order_by(Account.code)
    if entity_id is not None:
        stmt = stmt.where(Account.entity_id == entity_id)
    result = await db.execute(stmt)
    accounts = list(result.scalars().all())
    class_map = build_code_class_map(accounts)
    name_map = {str(a.code): a.name for a in accounts}
    return class_map, name_map


async def _load_scope_ids(db: AsyncSession) -> set[str]:
    """Postgres transaction ids in the posted/reversed set (the F2 scope)."""
    result = await db.execute(
        select(Transaction.id).where(Transaction.status.in_(IN_SCOPE_STATUSES))
    )
    return {str(row) for row in result.scalars().all()}


# ---------------------------------------------------------------------------
# Flows and balances
# ---------------------------------------------------------------------------


async def _period_flows(
    ledger: str,
    scope_ids: set[str],
    period_start: Optional[date],
    period_end: Optional[date],
) -> tuple[dict[str, int], dict[str, int]]:
    """Per-code (debit, credit) flows in pence for the scoped period.

    Scope: ``metadata.transaction_id`` in the Postgres posted/reversed set.
    Period: Python-side filter on the mirror ``timestamp`` (a missing or
    unparseable timestamp is treated as in-scope). For each posting the
    ``destination`` account is debited and the ``source`` account credited.
    """
    debit: dict[str, int] = defaultdict(int)
    credit: dict[str, int] = defaultdict(int)
    start, end = _period_window(period_start, period_end)
    for tx in await _list_all_transactions(ledger):
        if not isinstance(tx, dict):
            continue
        meta = tx.get("metadata")
        tx_id = meta.get("transaction_id") if isinstance(meta, dict) else None
        if not tx_id or str(tx_id) not in scope_ids:
            continue
        ts = _parse_timestamp(tx.get("timestamp"))
        if ts is not None:
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue
        for posting in tx.get("postings") or []:
            if not isinstance(posting, dict):
                continue
            try:
                amount = _decimal_to_pence(posting.get("amount"))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ReportEngineError(
                    f"unparseable posting amount {posting.get('amount')!r}: {exc}"
                ) from exc
            source = str(posting.get("source") or "")
            destination = str(posting.get("destination") or "")
            if source:
                credit[source] += amount
            if destination:
                debit[destination] += amount
    return dict(debit), dict(credit)


def _check_flow_codes(
    debit: dict[str, int], credit: dict[str, int], class_map: dict[str, str]
) -> None:
    """Classification gap: any Formance code absent from the COA raises."""
    unknown = sorted((set(debit) | set(credit)) - set(class_map))
    if unknown:
        raise ReportEngineError(
            "unknown account codes in Formance transactions: " + ", ".join(unknown)
        )


async def _ledger_balances(
    ledger: str, class_map: dict[str, str]
) -> dict[str, int]:
    """Per-code Formance balances in pence (debits − credits), classified."""
    balances = _parse_balances(await formance_client.get_balances(ledger))
    unknown = sorted(set(balances) - set(class_map))
    if unknown:
        raise ReportEngineError(
            "unknown account codes in Formance balances: " + ", ".join(unknown)
        )
    return balances


async def _pl_net_flows(
    db: AsyncSession,
    ledger: str,
    entity_id: Optional[uuid.UUID],
    period_start: Optional[date],
    period_end: Optional[date],
) -> tuple[
    dict[str, int], dict[str, int], dict[str, str], dict[str, str]
]:
    """P&L-period per-code (debit, credit) flows + the entity COA maps.

    Shared by ``generate_pl`` and ``generate_bs`` (Contract F24) so the
    balance-sheet current-profit line is computed from the EXACT same
    data as the P&L report: the entity-scoped COA classification, the
    posted/reversed scope, the period window, and the unknown-code check.
    Returns ``(debit, credit, class_map, name_map)``.
    """
    class_map, name_map = await _load_coa(db, entity_id=entity_id)
    scope_ids = await _load_scope_ids(db)
    debit, credit = await _period_flows(ledger, scope_ids, period_start, period_end)
    _check_flow_codes(debit, credit, class_map)
    return debit, credit, class_map, name_map


# ---------------------------------------------------------------------------
# P&L computation (shared by generate_pl / cash flow / funds)
# ---------------------------------------------------------------------------


def _pl_from_flows(
    debit: dict[str, int],
    credit: dict[str, int],
    class_map: dict[str, str],
    name_map: dict[str, str],
) -> dict[str, Any]:
    """F8 P&L recipe over per-code flows. Pure — raises on identity breaks."""

    def net(code: str) -> int:
        return debit.get(code, 0) - credit.get(code, 0)

    def credit_net(code: str) -> int:
        return credit.get(code, 0) - debit.get(code, 0)

    # Table-driven placement: the F8 line-item tables are the spec (bound
    # statically in the unit tests at GROUP level). A tabled code joins its
    # group only when its COA class matches; codes outside every table fall
    # back to class-based placement so client COAs with extra accounts keep
    # working.
    tabled = (
        set(_REVENUE_CODES)
        | set(_DIRECT_CODES)
        | set(_OVERHEAD_CODES)
        | set(_DEPRECIATION_CODES)
        | set(_STAFF_CODES)
        | set(_FINANCE_CODES)
    )
    revenue_codes = sorted(
        {c for c in _REVENUE_CODES if class_map.get(c) == "Revenue (Revenue)"}
        | {
            c
            for c in class_map
            if class_map[c] == "Revenue (Revenue)" and c not in tabled
        }
    )
    direct_codes = sorted(
        {c for c in _DIRECT_CODES if class_map.get(c) == "Expense (DirectCost)"}
        | {
            c
            for c in class_map
            if class_map[c] == "Expense (DirectCost)" and c not in tabled
        }
    )
    expense_codes = sorted(
        c for c in class_map if class_map[c] == "Expense (Expense)"
    )
    depreciation_codes = sorted(
        c for c in _DEPRECIATION_CODES if c in expense_codes
    )
    staff_codes = sorted(c for c in _STAFF_CODES if c in expense_codes)
    finance_codes = sorted(c for c in _FINANCE_CODES if c in expense_codes)
    special = set(_DEPRECIATION_CODES) | set(_STAFF_CODES) | set(_FINANCE_CODES)
    overhead_codes = sorted(
        {c for c in _OVERHEAD_CODES if c in expense_codes}
        | {c for c in expense_codes if c not in tabled and c not in special}
    )

    def lines(codes: list[str], sign_credit: bool = False) -> list[dict[str, Any]]:
        return [
            {
                "code": c,
                "name": name_map.get(c, c),
                "amount": credit_net(c) if sign_credit else net(c),
            }
            for c in codes
        ]

    revenue = lines(revenue_codes, sign_credit=True)
    direct = lines(direct_codes)
    overheads = lines(overhead_codes)
    staff = lines(staff_codes)
    depreciation = lines(depreciation_codes)
    finance = lines(finance_codes)

    # F8: charge = closing 2040 − opening 2040 + tax PAID (debit postings
    # to 2040) == the period's CREDIT flow on 2040 exactly.
    tax_charge = credit.get(_TAX_CODES[0], 0)

    revenue_total = sum(line["amount"] for line in revenue)
    direct_total = sum(line["amount"] for line in direct)
    overhead_total = sum(line["amount"] for line in overheads)
    staff_total = sum(line["amount"] for line in staff)
    depreciation_total = sum(line["amount"] for line in depreciation)
    finance_total = sum(line["amount"] for line in finance)

    gross_profit = revenue_total - direct_total
    operating_profit = (
        gross_profit - overhead_total - staff_total - depreciation_total
    )
    profit_before_tax = operating_profit - finance_total
    profit = profit_before_tax - tax_charge

    # The load-bearing assembly guard (:func:`_check_pl_guard`, run by
    # ``generate_pl``) compares each section subtotal above against the
    # independently computed raw per-class/per-code flow totals — a
    # section that silently drops a line item raises there.
    return {
        "sections": {
            "revenue": revenue,
            "direct_costs": direct,
            "overheads": overheads,
            "staff_costs": staff,
            "depreciation": depreciation,
            "finance_costs": finance,
            "corporation_tax": [
                {
                    "code": _TAX_CODES[0],
                    "name": name_map.get(_TAX_CODES[0], _TAX_CODES[0]),
                    "amount": tax_charge,
                }
            ],
        },
        "subtotals": {
            "gross_profit": gross_profit,
            "operating_profit": operating_profit,
            "profit_before_tax": profit_before_tax,
        },
        "totals": {
            "revenue": revenue_total,
            "direct_costs": direct_total,
            "expenses": (
                overhead_total + staff_total + depreciation_total + finance_total
            ),
            "corporation_tax": tax_charge,
        },
        "profit": profit,
    }


def _raw_pl_totals(
    debit: dict[str, int], credit: dict[str, int], class_map: dict[str, str]
) -> dict[str, int]:
    """Per-group P&L totals computed INDEPENDENTLY, straight from the flow
    data: per-class for revenue/direct costs/expenses, the gross credit
    flow on 2040 for tax. Never derived from the assembled line items —
    this is the reference the assembly guard checks against.
    """

    def net(code: str) -> int:
        return debit.get(code, 0) - credit.get(code, 0)

    def credit_net(code: str) -> int:
        return credit.get(code, 0) - debit.get(code, 0)

    revenue_total = sum(
        credit_net(c) for c in class_map if class_map[c] == "Revenue (Revenue)"
    )
    direct_total = sum(
        net(c) for c in class_map if class_map[c] == "Expense (DirectCost)"
    )
    expense_codes = [c for c in class_map if class_map[c] == "Expense (Expense)"]
    depreciation_total = sum(
        net(c) for c in expense_codes if c in _DEPRECIATION_CODES
    )
    staff_total = sum(net(c) for c in expense_codes if c in _STAFF_CODES)
    finance_total = sum(net(c) for c in expense_codes if c in _FINANCE_CODES)
    expense_total = sum(net(c) for c in expense_codes)
    return {
        "revenue": revenue_total,
        "direct_costs": direct_total,
        "overheads": expense_total - depreciation_total - staff_total - finance_total,
        "staff_costs": staff_total,
        "depreciation": depreciation_total,
        "finance_costs": finance_total,
        "corporation_tax": credit.get(_TAX_CODES[0], 0),
    }


def _assembled_pl_totals(body: dict[str, Any]) -> dict[str, int]:
    """Per-group subtotals summed from the ASSEMBLED line items."""
    return {
        group: sum(line["amount"] for line in body["sections"][group])
        for group in _RAW_PL_GROUPS
    }


def _check_pl_guard(
    debit: dict[str, int],
    credit: dict[str, int],
    class_map: dict[str, str],
    body: dict[str, Any],
) -> None:
    """Load-bearing assembly guard: every section subtotal must equal the
    independently computed raw flow total for its group. A section that
    silently drops a line item (mis-grouping) raises instead of returning
    a quietly wrong P&L."""
    raw = _raw_pl_totals(debit, credit, class_map)
    assembled = _assembled_pl_totals(body)
    for group in raw:
        if assembled[group] != raw[group]:
            raise ReportEngineError(
                f"P&L section misassembled: {group} assembled "
                f"{assembled[group]} != raw flow total {raw[group]}"
            )


# ---------------------------------------------------------------------------
# generate_pl — income statement from PERIOD FLOWS
# ---------------------------------------------------------------------------


async def generate_pl(
    db: AsyncSession,
    ledger: str = DEFAULT_LEDGER,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    entity_id: Optional[uuid.UUID] = None,
) -> dict[str, Any]:
    """F8 income statement from Formance period flows (never balances).

    The assembly guard compares each section subtotal against the
    independently computed per-class/per-code flow totals (revenue,
    direct costs, overheads, staff costs, depreciation, interest, tax)
    and raises :class:`ReportEngineError` when a section silently drops
    a line item — a broken P&L is never returned.
    """
    debit, credit, class_map, name_map = await _pl_net_flows(
        db, ledger, entity_id, period_start, period_end
    )
    body = _pl_from_flows(debit, credit, class_map, name_map)
    _check_pl_guard(debit, credit, class_map, body)
    return {
        "report": "profit_and_loss",
        "period": {"start": period_start, "end": period_end},
        **body,
        "identity_ok": True,
    }


# ---------------------------------------------------------------------------
# generate_bs — statement of financial position from BALANCES
# ---------------------------------------------------------------------------


async def generate_bs(
    db: AsyncSession,
    ledger: str = DEFAULT_LEDGER,
    as_at: Optional[date] = None,
    entity_id: Optional[uuid.UUID] = None,
    period_start: Optional[date] = None,
) -> dict[str, Any]:
    """F8 balance sheet from Formance balances (never period flows).

    Formance balance = debits − credits, so credit-nature balances are
    negated for presentation: liabilities/equity positive, ``1360``
    carried negative within assets; VAT always as one signed net liability
    line (F26).

    Contract F24: the equity section gains a flow-derived
    ``profit_for_period`` line — the P&L net over
    ``[period_start or date.min, as_at]`` using the SAME flow machinery as
    ``generate_pl`` (``_pl_net_flows`` + ``_pl_from_flows``) — and the
    identity is assets == liabilities + equity + current_profit, exact in
    pence. A genuinely broken ledger still raises
    :class:`ReportEngineError`.
    """
    as_at = as_at or date.today()
    # F24: the current-profit line reuses the P&L flow machinery — the
    # entity-scoped COA/class maps come from the same shared read.
    debit, credit, class_map, name_map = await _pl_net_flows(
        db, ledger, entity_id, period_start, as_at
    )
    current_profit_pence = _pl_from_flows(debit, credit, class_map, name_map)[
        "profit"
    ]
    balances = await _ledger_balances(ledger, class_map)

    def raw_section(codes: list[str]) -> list[dict[str, Any]]:
        return [
            {"code": c, "name": name_map.get(c, c), "amount": balances.get(c, 0)}
            for c in codes
        ]

    def neg_section(codes: list[str]) -> list[dict[str, Any]]:
        return [
            {"code": c, "name": name_map.get(c, c), "amount": -balances.get(c, 0)}
            for c in codes
        ]

    bank_codes = sorted(c for c in class_map if class_map[c] == "Asset (Bank)")
    current_asset_codes = sorted(
        c for c in class_map if class_map[c] == "Asset (CurrentAsset)"
    )
    fixed_cost_codes = sorted(
        c
        for c in class_map
        if class_map[c] == "Asset (FixedAsset)"
        and c not in _CONTRA_DEPRECIATION_CODES
    )
    current_liab_codes = sorted(
        c
        for c in class_map
        if class_map[c] == "Liability (CurrentLiability)" and c not in _VAT_CODES
    )
    lt_codes = sorted(
        c for c in class_map if class_map[c] == "Liability (LongTermLiability)"
    )
    equity_codes = sorted(
        c
        for c in class_map
        if class_map[c] == "Equity (Equity)" and c not in _CONTRA_EQUITY_CODES
    )

    bank_total = sum(balances.get(c, 0) for c in bank_codes)
    current_assets_total = sum(balances.get(c, 0) for c in current_asset_codes)
    fixed_net = sum(balances.get(c, 0) for c in fixed_cost_codes) + sum(
        balances.get(c, 0) for c in _CONTRA_DEPRECIATION_CODES
    )
    vat_net = -sum(balances.get(c, 0) for c in _VAT_CODES)

    current_liab_total = -sum(balances.get(c, 0) for c in current_liab_codes)
    lt_total = -sum(balances.get(c, 0) for c in lt_codes)
    equity_total = -sum(balances.get(c, 0) for c in equity_codes) - sum(
        balances.get(c, 0) for c in _CONTRA_EQUITY_CODES
    )

    # F26: VAT is ALWAYS presented as one signed net line within
    # liabilities (never reclassified to a 1130 recoverable asset) so the
    # engine and the Postgres ReportService measure the same thing and the
    # cross-check is exact. A negative net (recoverable) simply reduces
    # liabilities.
    total_assets = bank_total + current_assets_total + fixed_net
    total_liabilities = current_liab_total + vat_net + lt_total
    equity_with_profit = equity_total + current_profit_pence
    liabilities_and_equity = total_liabilities + equity_with_profit

    if total_assets != liabilities_and_equity:
        raise ReportEngineError(
            f"balance sheet identity violated: assets {total_assets} != "
            f"liabilities {total_liabilities} + equity {equity_total} "
            f"+ current profit {current_profit_pence} "
            f"= {liabilities_and_equity}"
        )

    sections = {
        "bank": raw_section(bank_codes),
        "current_assets": raw_section(current_asset_codes),
        "fixed_assets": raw_section(fixed_cost_codes),
        "accumulated_depreciation": raw_section(list(_CONTRA_DEPRECIATION_CODES)),
        "current_liabilities": neg_section(current_liab_codes)
        + [
            {
                "code": "VAT",
                "name": "VAT net (2210 - 2220)",
                "amount": vat_net,
            }
        ],
        "long_term_liabilities": neg_section(lt_codes),
        "equity": neg_section(equity_codes) + neg_section(list(_CONTRA_EQUITY_CODES)),
    }
    # F24: current-year profit/loss closes the equity section (flow-derived;
    # the equity ACCOUNTS alone no longer need to absorb P&L flows).
    sections["equity"].append(
        {
            "code": "profit_for_period",
            "name": "Profit for the period",
            "amount": current_profit_pence,
        }
    )

    return {
        "report": "balance_sheet",
        "as_at": as_at,
        "sections": sections,
        "vat_net": vat_net,
        "totals": {
            "assets": total_assets,
            "liabilities": total_liabilities,
            "equity": equity_with_profit,
            "liabilities_and_equity": liabilities_and_equity,
        },
        "identity_ok": True,
    }


# ---------------------------------------------------------------------------
# generate_tb — trial balance from BALANCES, two columns by sign
# ---------------------------------------------------------------------------


async def generate_tb(
    db: AsyncSession,
    ledger: str = DEFAULT_LEDGER,
    as_at: Optional[date] = None,
    entity_id: Optional[uuid.UUID] = None,
) -> dict[str, Any]:
    """F8 trial balance from Formance balances.

    balance = debits − credits; balance > 0 → debit column, balance < 0 →
    credit column (positive). Identity: Σ debits == Σ credits, exact.
    """
    as_at = as_at or date.today()
    class_map, name_map = await _load_coa(db, entity_id=entity_id)
    balances = await _ledger_balances(ledger, class_map)

    accounts: list[dict[str, Any]] = []
    total_debits = 0
    total_credits = 0
    for code in sorted(balances):
        value = balances[code]
        dr = value if value > 0 else 0
        cr = -value if value < 0 else 0
        total_debits += dr
        total_credits += cr
        accounts.append(
            {
                "code": code,
                "name": name_map.get(code, code),
                "class": class_map[code],
                "balance": value,
                "debit": dr,
                "credit": cr,
            }
        )

    if total_debits != total_credits:
        raise ReportEngineError(
            f"trial balance does not balance: debits {total_debits} != "
            f"credits {total_credits} (difference {total_debits - total_credits})"
        )

    return {
        "report": "trial_balance",
        "as_at": as_at,
        "accounts": accounts,
        "total_debits": total_debits,
        "total_credits": total_credits,
        "identity_ok": True,
    }


# ---------------------------------------------------------------------------
# Contract F37 — ledger-derived AR/AP aging
# ---------------------------------------------------------------------------


def _aging_bucket(days_overdue: int) -> str:
    """Aging bucket for a day count (future-dated items clamp into 0-30)."""
    days = max(int(days_overdue), 0)
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


async def _aging_legs(
    db: AsyncSession,
    code: str,
    entity_id: Optional[uuid.UUID],
) -> list[dict[str, Any]]:
    """Per-transaction legs on account ``code`` (posted + reversed scope).

    Reads the POSTED Postgres transaction set (the F2 scope discipline:
    ``posted`` + ``reversed``), entity-scoped, joining postings, and
    aggregates each matching transaction into its NET movement on the
    account: ``debit_pence`` / ``credit_pence`` are the sums of that
    transaction's postings to this account. Ordered by effective_date
    ascending (then id) — the FIFO allocation order. Integer pence only.
    """
    account_stmt = select(Account.id).where(
        Account.code == code, Account.is_active.is_(True)
    )
    if entity_id is not None:
        account_stmt = account_stmt.where(Account.entity_id == entity_id)
    account_ids = set((await db.execute(account_stmt)).scalars().all())
    if not account_ids:
        return []

    stmt = (
        select(Transaction)
        .where(Transaction.status.in_(IN_SCOPE_STATUSES))
        .where(
            Transaction.id.in_(
                select(Posting.transaction_id).where(
                    Posting.account_id.in_(account_ids)
                )
            )
        )
        .options(selectinload(Transaction.postings))
    )
    if entity_id is not None:
        stmt = stmt.where(Transaction.entity_id == entity_id)
    transactions = list((await db.execute(stmt)).scalars().all())

    legs: list[dict[str, Any]] = []
    for tx in transactions:
        debit = sum(
            p.debit_amount for p in (tx.postings or []) if p.account_id in account_ids
        )
        credit = sum(
            p.credit_amount for p in (tx.postings or []) if p.account_id in account_ids
        )
        if debit == 0 and credit == 0:
            continue
        legs.append(
            {
                "transaction_id": tx.id,
                "reference": tx.reference,
                "date": tx.effective_date,
                "debit_pence": debit,
                "credit_pence": credit,
            }
        )
    legs.sort(
        key=lambda leg: (
            leg["date"] is None,
            leg["date"] or date.min,
            str(leg["transaction_id"]),
        )
    )
    return legs


def _fifo_age_legs(
    legs: list[dict[str, Any]], as_at: date, *, items_on_debit: bool
) -> tuple[list[dict[str, Any]], int]:
    """FIFO-age ordered legs: (items, total_outstanding_pence).

    AR (``items_on_debit=True``): the item moments are the legs whose net
    movement DEBITS 1100 (the invoice); settlements are the net-credit
    legs (customer receipts). AP mirrors it: item moments are the net
    CREDITS to 2000 (the supplier invoice), settlements the net debits
    (supplier payments). Every settlement is allocated FIFO against the
    oldest outstanding item remainder — exact pence, partial allocations
    allowed. A leg that both debits and credits the account nets into one
    movement.
    """
    items: list[dict[str, Any]] = []
    settlements: list[dict[str, Any]] = []
    for leg in legs:
        net = leg["debit_pence"] - leg["credit_pence"]
        is_item = net > 0 if items_on_debit else net < 0
        if is_item:
            amount = abs(net)
            items.append(
                {
                    "ref": leg["reference"] or str(leg["transaction_id"])[:8],
                    "date": leg["date"],
                    "original_pence": amount,
                    "paid_pence": 0,
                    "outstanding_pence": amount,
                    "days_overdue": (as_at - (leg["date"] or as_at)).days,
                }
            )
        elif net != 0:
            settlements.append({"amount": abs(net)})

    next_item = 0
    for settlement in settlements:
        remaining = settlement["amount"]
        while remaining > 0 and next_item < len(items):
            item = items[next_item]
            allocation = min(remaining, item["outstanding_pence"])
            item["paid_pence"] += allocation
            item["outstanding_pence"] -= allocation
            remaining -= allocation
            if item["outstanding_pence"] == 0:
                next_item += 1

    total = sum(item["outstanding_pence"] for item in items)
    return items, total


async def _generate_aging(
    db: AsyncSession,
    ledger: str,
    as_at: Optional[date],
    entity_id: Optional[uuid.UUID],
    *,
    report_name: str,
) -> dict[str, Any]:
    """Shared F37 AR/AP aging core (ledger-derived, TB-reconciled).

    Every posted/reversed transaction is aged — there is no as-at cut-off
    on the Postgres side because the reconciliation target (the Formance
    balance read) has no as-of date either: filtering one side would break
    the equality by construction.
    """
    as_at = as_at or date.today()
    code, label = _AGING_DEFS[report_name]
    class_map, name_map = await _load_coa(db, entity_id=entity_id)
    balances = await _ledger_balances(ledger, class_map)
    balance = balances.get(code, 0)

    legs = await _aging_legs(db, code, entity_id)
    items, total = _fifo_age_legs(
        legs, as_at, items_on_debit=report_name == "aged_receivables"
    )

    # Sign-aware reconciliation (verifier fix-list): AR must sit debit,
    # AP must sit credit, unless the account is flat.
    if total != 0:
        expected_debit = report_name == "aged_receivables"
        if (balance > 0) != expected_debit:
            raise ReportEngineError(
                f"{report_name} sign mismatch: {code} balance {balance} "
                f"is on the wrong side for outstanding {total} — a net "
                f"credit balance on a receivables account (overpayment or "
                f"credit note) is not an aged-receivable report"
            )

    if total != abs(balance):
        raise ReportEngineError(
            f"{report_name} does not reconcile to the trial balance: item "
            f"outstanding {total} != |{code} balance| {abs(balance)} "
            f"(the aging must be derived from the SAME ledger the Balance "
            f"Sheet reads)"
        )

    buckets = [
        {"bucket": bucket, "count": 0, "total_pence": 0} for bucket in AGING_BUCKETS
    ]
    by_bucket = {bucket["bucket"]: bucket for bucket in buckets}
    for item in items:
        if item["outstanding_pence"] <= 0:
            continue
        bucket = by_bucket[_aging_bucket(item["days_overdue"])]
        bucket["count"] += 1
        bucket["total_pence"] += item["outstanding_pence"]

    return {
        "report": report_name,
        "as_at": as_at,
        "items": items,
        "buckets": buckets,
        "total_outstanding_pence": total,
        "ledger_balance": {
            "code": code,
            "name": name_map.get(code, label),
            "balance_pence": balance,
        },
        "reconciled": True,
        "note": _AGING_NOTES[report_name],
    }


async def generate_ar_aging(
    db: AsyncSession,
    ledger: str = DEFAULT_LEDGER,
    as_at: Optional[date] = None,
    entity_id: Optional[uuid.UUID] = None,
) -> dict[str, Any]:
    """Aged receivables from the POSTED ledger (Contract F37).

    Invoice moments (net debits to 1100) FIFO-matched against customer
    receipts (net credits to 1100) over the F2 posted + reversed scope,
    entity-scoped. ``total_outstanding_pence`` must equal the absolute
    trial-balance balance for 1100 — otherwise
    :class:`ReportEngineError` (a broken aging is never returned).
    """
    return await _generate_aging(
        db, ledger, as_at, entity_id, report_name="aged_receivables"
    )


async def generate_ap_aging(
    db: AsyncSession,
    ledger: str = DEFAULT_LEDGER,
    as_at: Optional[date] = None,
    entity_id: Optional[uuid.UUID] = None,
) -> dict[str, Any]:
    """Aged payables: the AR mirror on 2000 (Contract F37).

    Supplier-invoice moments are the net CREDITS to 2000, supplier
    payments the net DEBITS; same FIFO allocation and TB reconciliation
    (absolute 2000 balance) as :func:`generate_ar_aging`.
    """
    return await _generate_aging(
        db, ledger, as_at, entity_id, report_name="aged_payables"
    )


# ---------------------------------------------------------------------------
# generate_cash_flow — indirect method over PERIOD FLOWS
# ---------------------------------------------------------------------------


async def generate_cash_flow(
    db: AsyncSession,
    ledger: str = DEFAULT_LEDGER,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    entity_id: Optional[uuid.UUID] = None,
) -> dict[str, Any]:
    """F8 indirect cash flow from period flows + current bank balances.

    Identity: operating + investing + financing == change in bank (the
    period net bank flow), exact in pence. Raises
    :class:`ReportEngineError` when the identity breaks — e.g. closing
    entries inside the period or tax provisions posted outside 3020.
    """
    class_map, name_map = await _load_coa(db, entity_id=entity_id)
    scope_ids = await _load_scope_ids(db)
    debit, credit = await _period_flows(ledger, scope_ids, period_start, period_end)
    _check_flow_codes(debit, credit, class_map)
    balances = await _ledger_balances(ledger, class_map)

    pl = _pl_from_flows(debit, credit, class_map, name_map)
    profit = pl["profit"]
    tax = pl["totals"]["corporation_tax"]

    def net(codes: tuple[str, ...]) -> int:
        return sum(debit.get(c, 0) - credit.get(c, 0) for c in codes)

    def credit_net(codes: tuple[str, ...]) -> int:
        return sum(credit.get(c, 0) - debit.get(c, 0) for c in codes)

    dep = net(_DEPRECIATION_CODES)

    operating_lines: list[dict[str, Any]] = [
        {"code": "PROFIT", "name": "Profit for the period", "amount": profit},
        {
            "code": _DEPRECIATION_CODES[0],
            "name": f"Add back: {name_map.get(_DEPRECIATION_CODES[0], _DEPRECIATION_CODES[0])}",
            "amount": dep,
        },
    ]
    wc_total = 0
    for c in _WC_ASSET_CODES:
        amount = -net((c,))
        wc_total += amount
        operating_lines.append(
            {
                "code": c,
                "name": f"Increase in {name_map.get(c, c)} ({c})",
                "amount": amount,
            }
        )
    other_ca_codes = sorted(
        c
        for c in class_map
        if class_map[c] == "Asset (CurrentAsset)" and c not in _WC_ASSET_CODES
    )
    other_ca_delta = -net(tuple(other_ca_codes))
    wc_total += other_ca_delta
    if other_ca_codes:
        operating_lines.append(
            {
                "code": "+".join(other_ca_codes),
                "name": "Increase in other current assets",
                "amount": other_ca_delta,
            }
        )
    wc_liab_codes = sorted(
        c
        for c in class_map
        if class_map[c] == "Liability (CurrentLiability)" and c not in _VAT_CODES
    )
    for c in wc_liab_codes:
        amount = credit_net((c,))
        wc_total += amount
        operating_lines.append(
            {
                "code": c,
                "name": f"Increase in {name_map.get(c, c)} ({c})",
                "amount": amount,
            }
        )
    vat_delta = credit_net(_VAT_CODES)
    wc_total += vat_delta
    operating_lines.append(
        {
            "code": "VAT",
            "name": "Increase in net VAT liability",
            "amount": vat_delta,
        }
    )
    operating = profit + dep + wc_total

    fixed_codes = sorted(
        c
        for c in class_map
        if class_map[c] == "Asset (FixedAsset)"
        and c not in _CONTRA_DEPRECIATION_CODES
    )
    fixed_purchases = net(tuple(fixed_codes))
    investing_lines = [
        {
            "code": "1300-1350",
            "name": "Fixed asset purchases",
            "amount": -fixed_purchases,
        }
    ]
    investing = -fixed_purchases

    lt_codes = sorted(
        c for c in class_map if class_map[c] == "Liability (LongTermLiability)"
    )
    borrowings = credit_net(tuple(lt_codes))
    capital = credit_net(("3000",))
    dividends = net(_DIVIDEND_CODES)
    financing_lines = [
        {"code": "2100", "name": "Borrowings (net drawdowns)", "amount": borrowings},
        {"code": "3000", "name": "Share capital introduced", "amount": capital},
        {
            "code": "+".join(_DIVIDEND_CODES),
            "name": "Dividends / drawings paid",
            "amount": -dividends,
        },
    ]
    financing = borrowings + capital - dividends

    bank_codes = sorted(c for c in class_map if class_map[c] == "Asset (Bank)")
    bank_flow = net(tuple(bank_codes))

    net_change = operating + investing + financing
    if net_change != bank_flow:
        flow_3020 = debit.get("3020", 0) - credit.get("3020", 0)
        raise ReportEngineError(
            f"cash flow identity violated: operating {operating} + investing "
            f"{investing} + financing {financing} = {net_change} != change in "
            f"bank {bank_flow} (unexplained movement — closing entries or "
            f"direct 3020 postings inside the period; 3020 flow {flow_3020}, "
            f"tax charge {tax})"
        )

    bank_closing = sum(balances.get(c, 0) for c in bank_codes)
    bank_opening = bank_closing - bank_flow
    if bank_closing - bank_opening != bank_flow:
        raise ReportEngineError(
            f"cash flow bank reconciliation failed: closing {bank_closing} - "
            f"opening {bank_opening} != net flow {bank_flow}"
        )

    return {
        "report": "cash_flow",
        "period": {"start": period_start, "end": period_end},
        "sections": {
            "operating": operating_lines,
            "investing": investing_lines,
            "financing": financing_lines,
        },
        "subtotals": {
            "operating": operating,
            "investing": investing,
            "financing": financing,
        },
        "reconciliation": {
            "net_change_in_cash": net_change,
            "bank_opening": bank_opening,
            "bank_closing": bank_closing,
        },
        "identity_ok": True,
    }


# ---------------------------------------------------------------------------
# generate_funds — sources and application of funds
# ---------------------------------------------------------------------------


async def generate_funds(
    db: AsyncSession,
    ledger: str = DEFAULT_LEDGER,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    entity_id: Optional[uuid.UUID] = None,
) -> dict[str, Any]:
    """F8 funds statement: long-term sources vs applications + WC movement.

    Identities (exact in pence): Σ sources == Σ applications, and the
    working-capital movement == Δ current assets − Δ current liabilities
    (period net flows). A working-capital DECREASE is presented as a source.
    """
    class_map, name_map = await _load_coa(db, entity_id=entity_id)
    scope_ids = await _load_scope_ids(db)
    debit, credit = await _period_flows(ledger, scope_ids, period_start, period_end)
    _check_flow_codes(debit, credit, class_map)

    pl = _pl_from_flows(debit, credit, class_map, name_map)
    profit = pl["profit"]

    def net(codes: tuple[str, ...]) -> int:
        return sum(debit.get(c, 0) - credit.get(c, 0) for c in codes)

    def credit_net(codes: tuple[str, ...]) -> int:
        return sum(credit.get(c, 0) - debit.get(c, 0) for c in codes)

    dep = net(_DEPRECIATION_CODES)
    lt_codes = sorted(
        c for c in class_map if class_map[c] == "Liability (LongTermLiability)"
    )
    # F8: drawdowns are long-term SOURCES (credit flow) and repayments are
    # APPLICATIONS (debit flow) — gross flows, not netted.
    borrowings = sum(credit.get(c, 0) for c in lt_codes)
    repayments = sum(debit.get(c, 0) for c in lt_codes)
    capital = credit.get("3000", 0)

    fixed_codes = sorted(
        c
        for c in class_map
        if class_map[c] == "Asset (FixedAsset)"
        and c not in _CONTRA_DEPRECIATION_CODES
    )
    fixed_purchases = sum(debit.get(c, 0) for c in fixed_codes)
    dividends = sum(debit.get(c, 0) for c in _DIVIDEND_CODES)

    long_sources = profit + dep + borrowings + capital
    long_applications = fixed_purchases + repayments + dividends
    wc_movement = long_sources - long_applications

    ca_codes = sorted(
        c
        for c in class_map
        if class_map[c] in ("Asset (Bank)", "Asset (CurrentAsset)")
    )
    cl_codes = sorted(
        c for c in class_map if class_map[c] == "Liability (CurrentLiability)"
    )
    ca_delta = net(tuple(ca_codes))
    cl_delta = credit_net(tuple(cl_codes))
    wc_split = ca_delta - cl_delta
    if wc_split != wc_movement:
        raise ReportEngineError(
            f"funds identity violated: working-capital movement {wc_movement} "
            f"!= Δcurrent assets {ca_delta} - Δcurrent liabilities "
            f"{cl_delta} = {wc_split}"
        )

    sources = [
        {"code": "PROFIT", "name": "Profit for the period", "amount": profit},
        {
            "code": _DEPRECIATION_CODES[0],
            "name": "Depreciation (non-fund charge)",
            "amount": dep,
        },
        {"code": "2100", "name": "Long-term borrowings", "amount": borrowings},
        {"code": "3000", "name": "Capital introduced", "amount": capital},
    ]
    applications = [
        {
            "code": "1300-1350",
            "name": "Fixed asset purchases",
            "amount": fixed_purchases,
        },
        {"code": "2100", "name": "Loan repayments", "amount": repayments},
        {
            "code": "+".join(_DIVIDEND_CODES),
            "name": "Drawings / dividends",
            "amount": dividends,
        },
    ]
    if wc_movement >= 0:
        applications.append(
            {
                "code": "WC",
                "name": "Increase in working capital",
                "amount": wc_movement,
            }
        )
    else:
        sources.append(
            {
                "code": "WC",
                "name": "Decrease in working capital (source)",
                "amount": -wc_movement,
            }
        )

    sources_total = sum(line["amount"] for line in sources)
    applications_total = sum(line["amount"] for line in applications)
    if sources_total != applications_total:
        raise ReportEngineError(
            f"funds identity violated: sources {sources_total} != "
            f"applications {applications_total}"
        )

    return {
        "report": "funds",
        "period": {"start": period_start, "end": period_end},
        "sources": sources,
        "applications": applications,
        "totals": {"sources": sources_total, "applications": applications_total},
        "working_capital_split": {
            "current_assets": ca_delta,
            "current_liabilities": cl_delta,
            "net": wc_split,
        },
        "identity_ok": True,
    }


# ---------------------------------------------------------------------------
# Cross-check against the Postgres ReportService
# ---------------------------------------------------------------------------


async def verify_against_postgres(
    db: AsyncSession,
    report_dict: dict[str, Any],
    report_type: str,
    entity_id: Optional[uuid.UUID] = None,
) -> list[dict[str, Any]]:
    """Compare a Formance-derived report with the Postgres ``ReportService``.

    Returns a list of per-line diffs (``{"report_type", "line", "engine",
    "postgres", "delta"}``); an empty list means exact agreement. Only
    P&L, BS and TB have Postgres-side equivalents; ``"cash_flow"`` and
    ``"funds"`` return an empty list.

    ``entity_id`` (Contract F20) passes the report scope through to the
    Postgres engine so both sides of the cross-check read the SAME
    entity's rows — two entities with posted data never produce false
    cross-entity drift.  ``None`` keeps the legacy global comparison.

    Contract F24: the BS comparison covers exactly the sections both
    engines compute identically — current assets (bank + current assets),
    fixed assets (fixed + accumulated depreciation), current liabilities,
    long-term liabilities, the equity ACCOUNTS (excluding the
    ``profit_for_period`` line, which ``ReportService.run_bs`` has no
    equivalent for — its equity subtotal is the equity accounts only),
    total assets, and total liabilities. The profit line itself is not
    compared. (The F8 VAT reclassification into ``1130`` remains an
    intentional engine-only difference outside this comparison.)
    """
    from src.services.report_service import ReportService
    from src.validators.report import ReportRunRequest

    diffs: list[dict[str, Any]] = []

    def diff(line: str, engine: int, postgres: int) -> None:
        if engine != postgres:
            diffs.append(
                {
                    "report_type": report_type,
                    "line": line,
                    "engine": engine,
                    "postgres": postgres,
                    "delta": engine - postgres,
                }
            )

    rt = str(report_type).lower().replace("-", "_")

    if rt in ("pl", "profit_and_loss"):
        period = report_dict["period"]
        req = ReportRunRequest(
            template_name="profit_and_loss",
            start_date=period["start"],
            end_date=period["end"],
            entity_id=entity_id,
        )
        pg = await ReportService.run_pl(db, req, entity_id=entity_id)
        totals = report_dict["totals"]
        diff("revenue", totals["revenue"], pg.revenue.subtotal)
        diff("direct_costs", totals["direct_costs"], pg.direct_costs.subtotal)
        diff("expenses", totals["expenses"], pg.expenses.subtotal)
        diff("net_profit", report_dict["profit"], pg.net_profit)
        return diffs

    if rt in ("bs", "balance_sheet"):
        as_at = report_dict["as_at"]
        req = ReportRunRequest(
            template_name="balance_sheet",
            start_date=as_at,
            end_date=as_at,
            entity_id=entity_id,
        )
        pg = await ReportService.run_bs(db, req, entity_id=entity_id)
        totals = report_dict["totals"]
        sections = report_dict["sections"]
        engine_ca = sum(line["amount"] for line in sections["bank"]) + sum(
            line["amount"] for line in sections["current_assets"]
        )
        engine_fa = sum(line["amount"] for line in sections["fixed_assets"]) + sum(
            line["amount"] for line in sections["accumulated_depreciation"]
        )
        engine_cl = sum(line["amount"] for line in sections["current_liabilities"])
        engine_ll = sum(
            line["amount"] for line in sections["long_term_liabilities"]
        )
        # F24: run_bs has no current-profit line — compare the equity
        # ACCOUNTS only, excluding profit_for_period from the engine side.
        engine_eq = sum(
            line["amount"]
            for line in sections["equity"]
            if line["code"] != "profit_for_period"
        )
        diff("current_assets", engine_ca, pg.current_assets.subtotal)
        diff("fixed_assets", engine_fa, pg.fixed_assets.subtotal)
        diff("current_liabilities", engine_cl, pg.current_liabilities.subtotal)
        diff("long_term_liabilities", engine_ll, pg.long_term_liabilities.subtotal)
        diff("equity", engine_eq, pg.equity.subtotal)
        diff("total_assets", totals["assets"], pg.total_assets)
        diff("total_liabilities", totals["liabilities"], pg.total_liabilities)
        return diffs

    if rt in ("tb", "trial_balance"):
        as_at = report_dict["as_at"]
        req = ReportRunRequest(
            template_name="trial_balance",
            start_date=date.min,
            end_date=as_at,
            entity_id=entity_id,
        )
        pg = await ReportService.run_tb(db, req, entity_id=entity_id)
        engine_rows = {
            row["code"]: row["balance"] for row in report_dict["accounts"]
        }
        pg_rows = {line.account_code: line.net_amount for line in pg.accounts}
        for code in sorted(set(engine_rows) | set(pg_rows)):
            engine = engine_rows.get(code, 0)
            postgres = pg_rows.get(code, 0)
            diff(f"account {code}", engine, postgres)
        return diffs

    # cash flow / funds: no Postgres-side equivalent.
    return []
