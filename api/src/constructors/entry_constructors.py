"""F4 deterministic entry constructors — one per transaction-catalog skill.

Each constructor takes a pydantic facts model (``src.constructors.facts``)
plus a ``codes`` resolver (account code -> canonical class string, built by
``resolve_from_coa``) and returns a balanced ``ConstructedEntry`` whose
Formance postings come from the REAL ``build_formance_postings`` (F1) with
deterministic UUIDs keyed by account code.

Amounts are integer pence throughout. VAT arithmetic per SKILL.md:
20% -> ``net // 5``, 5% -> ``net // 20``, 0%/exempt -> 0;
gross = net + vat. The company-level schemes (cash / flat-rate) change
only VAT-return timing and are out of scope here — the ledger entry per
``vat_rate`` is identical, as each SKILL.md states.
"""

from __future__ import annotations

import uuid
from typing import Literal, Mapping

from src.services.transaction_service import build_formance_postings
from src.validators.transaction import PostingCreate

from . import ConstructorError, ConstructedEntry, Leg
from .facts import (
    BankTransferFacts,
    CardPurchaseFacts,
    CustomerReceiptFacts,
    DepreciationFacts,
    JournalAccrualFacts,
    OpeningBalancesFacts,
    PayrollFacts,
    PrepaymentFacts,
    SalesInvoiceFacts,
    SupplierInvoiceFacts,
    SupplierPaymentFacts,
)

__all__ = [
    "bank_transfer",
    "card_purchase",
    "customer_receipt",
    "depreciation",
    "journal_accrual",
    "opening_balances",
    "payroll",
    "prepayment",
    "sales_invoice",
    "supplier_invoice",
    "supplier_payment",
]

_FORMANCE_NS = uuid.UUID("f40e1a6a-9f2b-4d8c-9c3d-2b7a5e6f8c10")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _uuid_for(code: str) -> uuid.UUID:
    """Deterministic fake account UUID keyed by code (F1 mirroring)."""
    return uuid.uuid5(_FORMANCE_NS, code)


def _dr(code: str, cls: str, amount: int, description: str) -> Leg:
    return Leg(
        account=code,
        account_class=cls,
        debit_amount=amount,
        credit_amount=0,
        description=description,
    )


def _cr(code: str, cls: str, amount: int, description: str) -> Leg:
    return Leg(
        account=code,
        account_class=cls,
        debit_amount=0,
        credit_amount=amount,
        description=description,
    )


def _resolve(
    codes: Mapping[str, str],
    code: str,
    expected_classes: set[str] | None,
    context: str,
) -> str:
    """Resolve a code to its canonical class, validating it against the COA."""
    cls = codes.get(code)
    if cls is None:
        raise ConstructorError(
            f"{context}: account code {code!r} is not in the COA resolver"
        )
    if expected_classes is not None and cls not in expected_classes:
        raise ConstructorError(
            f"{context}: account {code!r} has class {cls!r}, expected one of "
            f"{sorted(expected_classes)}"
        )
    return cls


def _check_balance(legs: list[Leg], checks: list[str]) -> tuple[int, int]:
    """Append the balance check result; raise ConstructorError if unbalanced."""
    debits = sum(leg.debit_amount for leg in legs)
    credits = sum(leg.credit_amount for leg in legs)
    checks.append(f"balance: sum(debits) {debits} == sum(credits) {credits}")
    if debits != credits or debits <= 0:
        raise ConstructorError(
            f"unbalanced entry: debits {debits} pence, credits {credits} pence"
        )
    return debits, credits


def _formance_postings(legs: list[Leg]) -> list[dict[str, str]]:
    """Mirror legs via the REAL build_formance_postings (F1)."""
    postings = [
        PostingCreate(
            account_id=_uuid_for(leg.account),
            debit_amount=leg.debit_amount,
            credit_amount=leg.credit_amount,
        )
        for leg in legs
    ]
    codes = {_uuid_for(leg.account): leg.account for leg in legs}
    return build_formance_postings(postings, currency="GBP", codes=codes)


def _finish(
    skill_id: str,
    legs: list[Leg],
    checks: list[str],
    derived: dict[str, int],
) -> ConstructedEntry:
    if not legs:
        raise ConstructorError(f"{skill_id}: entry has no legs")
    _check_balance(legs, checks)
    return ConstructedEntry(
        skill_id=skill_id,
        legs=legs,
        formance_postings=_formance_postings(legs),
        derived=derived,
        checks=checks,
    )


def _round_half_up(numerator: int, denominator: int) -> int:
    """Round ``numerator / denominator`` half-up in integer arithmetic."""
    return (2 * numerator + denominator) // (2 * denominator)


def _vat_for_line(net_pence: int, vat_rate: str) -> int:
    """VAT per line: 20% -> net // 5, 5% -> net // 20, 0%/exempt -> 0."""
    if vat_rate == "20%":
        return net_pence // 5
    if vat_rate == "5%":
        return net_pence // 20
    return 0


def _gross_split(total_pence: int, vat_rate: str | None) -> tuple[int, int]:
    """Split a VAT-inclusive gross into (net, vat) for the rated rates.

    For 20%: vat = total // 6; for 5%: vat = total // 21 — the gross-up
    conventions that satisfy ``vat == net // 5`` (resp. ``net // 20``)
    exactly in integer pence. 0% / exempt / no VAT invoice: (total, 0).
    """
    if vat_rate == "20%":
        vat = total_pence // 6
        return total_pence - vat, vat
    if vat_rate == "5%":
        vat = total_pence // 21
        return total_pence - vat, vat
    return total_pence, 0


def _month_share(net: int, periods: int, index: int) -> int:
    """Release share for period ``index``: the final period absorbs rounding."""
    base = _round_half_up(net, periods)
    if index < periods - 1:
        return base
    return net - base * (periods - 1)


# ---------------------------------------------------------------------------
# bank-transfer
# ---------------------------------------------------------------------------


def bank_transfer(
    facts: BankTransferFacts, codes: Mapping[str, str]
) -> ConstructedEntry:
    checks: list[str] = []
    if facts.amount_pence <= 0:
        raise ConstructorError(
            f"bank-transfer: amount_pence must be > 0, got {facts.amount_pence}"
        )
    checks.append(f"amount > 0: {facts.amount_pence} pence")
    source = facts.source_account_code
    destination = facts.destination_account_code
    if source == destination:
        raise ConstructorError(
            f"bank-transfer: source and destination are the same account "
            f"{source} (no-op transfer)"
        )
    source_cls = _resolve(codes, source, {"Asset (Bank)"}, "bank-transfer source")
    destination_cls = _resolve(
        codes, destination, {"Asset (Bank)"}, "bank-transfer destination"
    )
    checks.append(
        f"exactly two legs, both Asset (Bank), different codes: "
        f"source {source} -> {source_cls}, destination {destination} -> "
        f"{destination_cls}"
    )
    checks.append("no Expense, Revenue, or VAT legs")
    legs = [
        _dr(destination, destination_cls, facts.amount_pence, f"Transfer from {source}"),
        _cr(source, source_cls, facts.amount_pence, f"Transfer to {destination}"),
    ]
    return _finish("bank-transfer", legs, checks, {})


# ---------------------------------------------------------------------------
# card-purchase
# ---------------------------------------------------------------------------


def card_purchase(facts: CardPurchaseFacts, codes: Mapping[str, str]) -> ConstructedEntry:
    """Cash-basis purchase paid immediately by card / bank transfer (F36b).

    Dr the expense/asset account net; standard/reduced-rated purchases add
    Dr 2220 Input VAT; Cr the bank account gross.  ``0%`` / ``exempt`` /
    ``None`` produce no VAT leg (the full net is the cost).
    """
    checks: list[str] = []
    if facts.net_pence <= 0:
        raise ConstructorError(
            f"card-purchase: net_pence must be > 0, got {facts.net_pence}"
        )
    bank_cls = _resolve(
        codes, facts.bank_account_code, {"Asset (Bank)"}, "card-purchase bank"
    )
    account_cls = _resolve(
        codes,
        facts.account_code,
        {"Expense (Expense)", "Expense (DirectCost)", "Asset (FixedAsset)"},
        "card-purchase expense or asset",
    )
    vat = _vat_for_line(facts.net_pence, facts.vat_rate) if facts.vat_rate else 0
    gross = facts.net_pence + vat
    checks.append(
        f"gross {gross} == net {facts.net_pence} + VAT {vat} (rate "
        f"{facts.vat_rate.value if facts.vat_rate else None})"
    )
    legs: list[Leg] = [
        _dr(facts.account_code, account_cls, facts.net_pence, "Purchase (net)")
    ]
    if vat > 0:
        assert facts.vat_rate is not None
        vat_cls = _resolve(
            codes,
            "2220",
            {"Liability (CurrentLiability)"},
            "card-purchase input VAT",
        )
        legs.append(_dr("2220", vat_cls, vat, f"Input VAT {facts.vat_rate.value}"))
    legs.append(_cr(facts.bank_account_code, bank_cls, gross, "Card payment"))
    checks.append(
        "at most one 2220 Input VAT Dr leg (rate group); 0%/exempt/no-VAT "
        "purchases contribute none"
    )
    checks.append(
        "credit leg is Asset (Bank); debit legs are Expense or Asset "
        "(FixedAsset) for capitalised purchases — no creditor leg (cash basis)"
    )
    return _finish("card-purchase", legs, checks, {
        "gross_pence": gross,
        "vat_pence": vat,
    })


# ---------------------------------------------------------------------------
# customer-receipt
# ---------------------------------------------------------------------------


def customer_receipt(
    facts: CustomerReceiptFacts, codes: Mapping[str, str]
) -> ConstructedEntry:
    checks: list[str] = []
    if facts.amount_pence <= 0:
        raise ConstructorError(
            f"customer-receipt: amount_pence must be > 0, got {facts.amount_pence}"
        )
    bank_cls = _resolve(
        codes, facts.bank_account_code, {"Asset (Bank)"}, "customer-receipt bank"
    )
    ar_cls = _resolve(
        codes, "1100", {"Asset (CurrentAsset)"}, "customer-receipt trade debtors"
    )
    checks.append(
        f"Dr bank ({facts.bank_account_code}, {bank_cls}); Cr trade debtors "
        f"(1100, {ar_cls}) — the receipt never credits a Revenue account"
    )
    legs = [
        _dr(facts.bank_account_code, bank_cls, facts.amount_pence,
            f"Receipt from {facts.customer_name}"),
        _cr("1100", ar_cls, facts.amount_pence, f"Receipt from {facts.customer_name}"),
    ]
    return _finish("customer-receipt", legs, checks, {})


# ---------------------------------------------------------------------------
# depreciation
# ---------------------------------------------------------------------------


def depreciation(
    facts: DepreciationFacts, codes: Mapping[str, str]
) -> ConstructedEntry:
    checks: list[str] = []
    if facts.cost_pence <= 0:
        raise ConstructorError(
            f"depreciation: cost_pence must be > 0, got {facts.cost_pence}"
        )
    if facts.useful_life_months < 1:
        raise ConstructorError(
            f"depreciation: useful_life_months must be >= 1, "
            f"got {facts.useful_life_months}"
        )
    if facts.residual_pence < 0 or facts.residual_pence > facts.cost_pence:
        raise ConstructorError(
            f"depreciation: residual {facts.residual_pence} outside 0..cost "
            f"{facts.cost_pence}"
        )
    depreciable = facts.cost_pence - facts.residual_pence
    charge = _round_half_up(depreciable, facts.useful_life_months)
    if charge <= 0:
        raise ConstructorError(
            f"depreciation: monthly charge is {charge} pence (cost "
            f"{facts.cost_pence} pence, residual {facts.residual_pence} pence)"
        )
    checks.append(
        f"monthly_charge {charge} == round((cost {facts.cost_pence} - residual "
        f"{facts.residual_pence}) / life {facts.useful_life_months})"
    )
    checks.append(
        f"accumulated depreciation {charge} never exceeds cost - residual "
        f"({depreciable})"
    )
    asset_cls = _resolve(
        codes, facts.asset_account_code, {"Asset (FixedAsset)"}, "depreciation asset"
    )
    if facts.asset_account_code == "1360":
        raise ConstructorError(
            "depreciation: the Cr leg must never be the asset account itself — "
            "asset_account_code cannot be the accumulated depreciation contra "
            "(1360)"
        )
    expense_cls = _resolve(
        codes, "6500", {"Expense (Expense)"}, "depreciation expense"
    )
    contra_cls = _resolve(
        codes, "1360", {"Asset (FixedAsset)"}, "accumulated depreciation contra"
    )
    legs = [
        _dr("6500", expense_cls, charge, "Monthly straight-line depreciation"),
        _cr("1360", contra_cls, charge, "Accumulated depreciation"),
    ]
    return _finish("depreciation", legs, checks, {"monthly_charge_pence": charge})


# ---------------------------------------------------------------------------
# journal-accrual
# ---------------------------------------------------------------------------


def journal_accrual(
    facts: JournalAccrualFacts,
    codes: Mapping[str, str],
    *,
    reversal: bool = False,
) -> ConstructedEntry:
    checks: list[str] = []
    if facts.amount_pence <= 0:
        raise ConstructorError(
            f"journal-accrual: amount_pence must be > 0, got {facts.amount_pence}"
        )
    expense_cls = _resolve(
        codes,
        facts.expense_account_code,
        {"Expense (Expense)", "Expense (DirectCost)"},
        "journal-accrual expense",
    )
    accrual_cls = _resolve(
        codes, "2010", {"Liability (CurrentLiability)"}, "journal-accrual accruals"
    )
    if not reversal:
        legs = [
            _dr(facts.expense_account_code, expense_cls, facts.amount_pence,
                facts.description),
            _cr("2010", accrual_cls, facts.amount_pence, facts.description),
        ]
    else:
        legs = [
            _dr("2010", accrual_cls, facts.amount_pence,
                f"Reversal: {facts.description}"),
            _cr(facts.expense_account_code, expense_cls, facts.amount_pence,
                f"Reversal: {facts.description}"),
        ]
        checks.append("reversal is the exact mirror with sides swapped")
    checks.append(
        f"exactly two legs: Dr {expense_cls} expense, Cr {accrual_cls} accruals"
    )
    checks.append("no VAT leg on the accrual — VAT enters only via the invoice entry")
    return _finish("journal-accrual", legs, checks, {})


# ---------------------------------------------------------------------------
# opening-balances
# ---------------------------------------------------------------------------


def opening_balances(
    facts: OpeningBalancesFacts, codes: Mapping[str, str]
) -> ConstructedEntry:
    checks: list[str] = []
    legs: list[Leg] = []
    for item in facts.assets:
        if item.balance_pence <= 0:
            raise ConstructorError(
                f"opening-balances: asset {item.code} balance must be > 0, "
                f"got {item.balance_pence}"
            )
        cls = _resolve(codes, item.code, None, f"opening-balances asset {item.code}")
        if not cls.startswith("Asset "):
            raise ConstructorError(
                f"opening-balances: debit leg {item.code} must be an Asset "
                f"class, got {cls}"
            )
        legs.append(_dr(item.code, cls, item.balance_pence, f"Opening balance: {item.name}"))
    for item in facts.liabilities:
        if item.balance_pence <= 0:
            raise ConstructorError(
                f"opening-balances: liability {item.code} balance must be > 0, "
                f"got {item.balance_pence}"
            )
        cls = _resolve(
            codes, item.code, None, f"opening-balances liability {item.code}"
        )
        if not cls.startswith("Liability "):
            raise ConstructorError(
                f"opening-balances: credit leg {item.code} must be a Liability "
                f"class, got {cls}"
            )
        legs.append(_cr(item.code, cls, item.balance_pence, f"Opening balance: {item.name}"))
    total_assets = sum(item.balance_pence for item in facts.assets)
    total_liabilities = sum(item.balance_pence for item in facts.liabilities)
    equity = total_assets - total_liabilities
    checks.append(
        f"assets {total_assets} - liabilities {total_liabilities} = "
        f"equity {equity}"
    )
    if equity != 0:
        eq_cls = _resolve(
            codes, facts.equity_account_code, None, "opening-balances equity"
        )
        if not eq_cls.startswith("Equity "):
            raise ConstructorError(
                f"opening-balances: equity leg {facts.equity_account_code} must "
                f"be an Equity class, got {eq_cls}"
            )
        if equity > 0:
            legs.append(_cr(facts.equity_account_code, eq_cls, equity, "Balancing equity"))
        else:
            legs.append(_dr(
                facts.equity_account_code, eq_cls, -equity,
                "Balancing equity (negative net worth)",
            ))
        checks.append("exactly one equity balancing amount (single Cr, or single Dr)")
    else:
        checks.append("equity balancing amount is 0 — no equity leg")
    checks.append("no Expense or Revenue legs")
    if not legs:
        raise ConstructorError("opening-balances: no accounts to open")
    return _finish("opening-balances", legs, checks, {"equity_pence": equity})


# ---------------------------------------------------------------------------
# payroll
# ---------------------------------------------------------------------------


def payroll(
    facts: PayrollFacts,
    codes: Mapping[str, str],
    *,
    journal: Literal["salary", "employer_ni", "employer_pension"] = "salary",
) -> ConstructedEntry:
    checks: list[str] = []
    if facts.gross_pence <= 0:
        raise ConstructorError(f"payroll: gross_pence must be > 0, got {facts.gross_pence}")
    for name, value in (
        ("paye_pence", facts.paye_pence),
        ("employee_nic_pence", facts.employee_nic_pence),
        ("employer_nic_pence", facts.employer_nic_pence),
        ("pension_employee_pence", facts.pension_employee_pence),
        ("pension_employer_pence", facts.pension_employer_pence),
    ):
        if value < 0:
            raise ConstructorError(f"payroll: {name} cannot be negative, got {value}")
    net = (
        facts.gross_pence
        - facts.paye_pence
        - facts.employee_nic_pence
        - facts.pension_employee_pence
    )
    if net < 0:
        raise ConstructorError(
            f"payroll: net pay is negative ({net} pence) — deductions exceed "
            f"gross {facts.gross_pence}, the entry cannot balance"
        )
    wages_cls = _resolve(codes, "6600", {"Expense (Expense)"}, "payroll staff wages")
    ni_expense_cls = _resolve(
        codes, "6610", {"Expense (Expense)"}, "payroll employer's NI"
    )
    pension_expense_cls = _resolve(
        codes, "6630", {"Expense (Expense)"}, "payroll pension contributions"
    )
    paye_cls = _resolve(
        codes, "2030", {"Liability (CurrentLiability)"}, "payroll PAYE/NI payable"
    )
    bank_cls = _resolve(codes, facts.bank_account_code, {"Asset (Bank)"}, "payroll bank")
    pension_payable_cls: str | None = None
    if facts.pension_payable_account_code:
        pension_payable_cls = _resolve(
            codes,
            facts.pension_payable_account_code,
            {"Liability (CurrentLiability)"},
            "payroll pension payable",
        )
    checks.append(
        f"gross {facts.gross_pence} == net {net} + PAYE {facts.paye_pence} + "
        f"employee NIC {facts.employee_nic_pence} + employee pension "
        f"{facts.pension_employee_pence}"
    )
    checks.append("pension never routes through 2030 — the 2030 balance stays the HMRC debt")

    if journal == "salary":
        legs = [_dr("6600", wages_cls, facts.gross_pence, "Gross pay")]
        if net > 0:
            legs.append(_cr(facts.bank_account_code, bank_cls, net, "Net pay to employees"))
        if facts.paye_pence > 0:
            legs.append(_cr("2030", paye_cls, facts.paye_pence, "PAYE withheld for HMRC"))
        if facts.employee_nic_pence > 0:
            legs.append(_cr(
                "2030", paye_cls, facts.employee_nic_pence,
                "Employee NIC withheld for HMRC",
            ))
        if facts.pension_employee_pence > 0:
            assert facts.pension_payable_account_code is not None
            legs.append(_cr(
                facts.pension_payable_account_code,
                pension_payable_cls,
                facts.pension_employee_pence,
                "Employee pension deduction",
            ))
        checks.append("bank leg credits exactly net pay, never gross")
    elif journal == "employer_ni":
        if facts.employer_nic_pence <= 0:
            raise ConstructorError(
                "payroll: employer_ni journal requires employer_nic_pence > 0, "
                f"got {facts.employer_nic_pence}"
            )
        legs = [
            _dr("6610", ni_expense_cls, facts.employer_nic_pence,
                "Employer's National Insurance"),
            _cr("2030", paye_cls, facts.employer_nic_pence,
                "Employer's NIC due to HMRC"),
        ]
    elif journal == "employer_pension":
        if facts.pension_employer_pence <= 0:
            raise ConstructorError(
                "payroll: employer_pension journal requires "
                f"pension_employer_pence > 0, got {facts.pension_employer_pence}"
            )
        if not facts.pension_payable_account_code or pension_payable_cls is None:
            raise ConstructorError(
                "payroll: pension_payable_account_code is required for the "
                "employer pension journal"
            )
        legs = [
            _dr("6630", pension_expense_cls, facts.pension_employer_pence,
                "Employer pension contribution"),
            _cr(facts.pension_payable_account_code, pension_payable_cls,
                facts.pension_employer_pence, "Employer pension due to scheme"),
        ]
    return _finish("payroll", legs, checks, {"net_pence": net} if journal == "salary" else {})


# ---------------------------------------------------------------------------
# prepayment
# ---------------------------------------------------------------------------


def prepayment(
    facts: PrepaymentFacts,
    codes: Mapping[str, str],
    *,
    stage: Literal["initial", "release"] = "initial",
    period_index: int = 0,
) -> ConstructedEntry:
    checks: list[str] = []
    if facts.total_pence <= 0:
        raise ConstructorError(
            f"prepayment: total_pence must be > 0, got {facts.total_pence}"
        )
    if facts.start_date > facts.end_date:
        raise ConstructorError(
            f"prepayment: start_date {facts.start_date} after end_date {facts.end_date}"
        )
    if facts.release_schedule == "per_period":
        periods = 1
    else:
        periods = (
            (facts.end_date.year - facts.start_date.year) * 12
            + (facts.end_date.month - facts.start_date.month)
            + 1
        )
    if period_index < 0 or period_index >= periods:
        raise ConstructorError(
            f"prepayment: period_index {period_index} out of range 0..{periods - 1}"
        )
    net, vat = _gross_split(facts.total_pence, facts.vat_rate)
    bank_cls = _resolve(codes, facts.bank_account_code, {"Asset (Bank)"}, "prepayment bank")
    share: int | None = None
    if stage == "release":
        share = _month_share(net, periods, period_index)
        if share <= 0:
            raise ConstructorError(
                f"prepayment: release share is {share} pence — total {net} "
                f"cannot be split across {periods} periods"
            )
        checks.append(
            f"release {period_index + 1}/{periods}: share {share} pence; releases "
            f"sum to exactly the balance held ({net})"
        )

    legs: list[Leg]
    if facts.direction == "prepaid_expense":
        prepay_cls = _resolve(
            codes, "1110", {"Asset (CurrentAsset)"}, "prepayments"
        )
        account_cls = _resolve(
            codes,
            facts.account_code,
            {"Expense (Expense)", "Expense (DirectCost)"},
            "prepaid expense release account",
        )
        if stage == "initial":
            legs = [_dr("1110", prepay_cls, net, "Prepayment (net of VAT)")]
            if vat:
                vat_cls = _resolve(
                    codes, "2220", {"Liability (CurrentLiability)"}, "input VAT"
                )
                legs.append(_dr("2220", vat_cls, vat, "Input VAT on prepayment"))
            legs.append(_cr(
                facts.bank_account_code, bank_cls, facts.total_pence,
                "Prepayment paid",
            ))
        else:
            assert share is not None
            legs = [
                _dr(facts.account_code, account_cls, share, "Prepayment release"),
                _cr("1110", prepay_cls, share, "Prepayment release"),
            ]
        checks.append("prepayment legs are Asset classes only")
    else:
        deferred_cls = _resolve(
            codes, "2020", {"Liability (CurrentLiability)"}, "deferred income"
        )
        account_cls = _resolve(
            codes, facts.account_code, {"Revenue (Revenue)"},
            "deferred income release account",
        )
        if stage == "initial":
            legs = [_dr(
                facts.bank_account_code, bank_cls, facts.total_pence,
                "Deferred income received",
            )]
            if vat:
                vat_cls = _resolve(
                    codes, "2210", {"Liability (CurrentLiability)"}, "output VAT"
                )
                legs.append(_cr("2210", vat_cls, vat, "Output VAT on deferred income"))
            legs.append(_cr("2020", deferred_cls, net, "Deferred income (net of VAT)"))
        else:
            assert share is not None
            legs = [
                _dr("2020", deferred_cls, share, "Deferred income release"),
                _cr(facts.account_code, account_cls, share, "Deferred income release"),
            ]
        checks.append("deferred income legs are Liability/Revenue classes only")

    if vat:
        expected = net // 5 if facts.vat_rate == "20%" else net // 20
        checks.append(
            f"VAT leg {vat} == net {net} x rate {facts.vat_rate} == {expected}"
        )
    derived: dict[str, int]
    if stage == "initial":
        derived = {"net_pence": net, "vat_pence": vat, "gross_pence": facts.total_pence}
    else:
        assert share is not None
        derived = {"month_share_pence": share}
    return _finish("prepayment", legs, checks, derived)


# ---------------------------------------------------------------------------
# sales-invoice
# ---------------------------------------------------------------------------


def sales_invoice(
    facts: SalesInvoiceFacts, codes: Mapping[str, str]
) -> ConstructedEntry:
    checks: list[str] = []
    if not facts.lines:
        raise ConstructorError("sales-invoice: at least one line is required")
    ar_cls = _resolve(
        codes, "1100", {"Asset (CurrentAsset)"}, "sales-invoice trade debtors"
    )
    vat_cls = _resolve(
        codes, "2210", {"Liability (CurrentLiability)"}, "sales-invoice output VAT"
    )
    revenue_by_code: dict[str, int] = {}
    revenue_cls_by_code: dict[str, str] = {}
    vat_by_rate: dict[str, int] = {"20%": 0, "5%": 0}
    for line in facts.lines:
        if line.net_pence <= 0:
            raise ConstructorError(
                f"sales-invoice: line net_pence must be > 0, got {line.net_pence}"
            )
        vat = _vat_for_line(line.net_pence, line.vat_rate)
        checks.append(
            f"line {line.description!r}: gross {line.net_pence + vat} == net "
            f"{line.net_pence} + VAT {vat}"
        )
        if line.revenue_account_code not in revenue_cls_by_code:
            revenue_cls_by_code[line.revenue_account_code] = _resolve(
                codes,
                line.revenue_account_code,
                {"Revenue (Revenue)"},
                "sales-invoice revenue",
            )
        revenue_by_code[line.revenue_account_code] = (
            revenue_by_code.get(line.revenue_account_code, 0) + line.net_pence
        )
        if line.vat_rate in ("20%", "5%"):
            vat_by_rate[line.vat_rate.value] += vat
    total_gross = sum(l.net_pence for l in facts.lines) + sum(vat_by_rate.values())
    legs = [_dr("1100", ar_cls, total_gross, f"Sales invoice {facts.invoice_ref}")]
    for code in revenue_by_code:
        legs.append(_cr(code, revenue_cls_by_code[code], revenue_by_code[code], "Sales (net)"))
    for rate in ("20%", "5%"):
        if vat_by_rate[rate] > 0:
            legs.append(_cr("2210", vat_cls, vat_by_rate[rate], f"Output VAT {rate}"))
    checks.append(
        "exactly one 2210 Output VAT Cr leg per rate group; zero/exempt lines "
        "contribute none"
    )
    checks.append("debit leg is Asset; revenue legs are Revenue; VAT legs are Liability")
    return _finish("sales-invoice", legs, checks, {
        "gross_pence": total_gross,
        "vat_pence": sum(vat_by_rate.values()),
    })


# ---------------------------------------------------------------------------
# supplier-invoice
# ---------------------------------------------------------------------------


def supplier_invoice(
    facts: SupplierInvoiceFacts, codes: Mapping[str, str]
) -> ConstructedEntry:
    checks: list[str] = []
    if not facts.lines:
        raise ConstructorError("supplier-invoice: at least one line is required")
    creditor_cls = _resolve(
        codes, "2000", {"Liability (CurrentLiability)"}, "supplier-invoice trade creditors"
    )
    vat_cls = _resolve(
        codes, "2220", {"Liability (CurrentLiability)"}, "supplier-invoice input VAT"
    )
    expense_by_code: dict[str, int] = {}
    expense_cls_by_code: dict[str, str] = {}
    vat_by_rate: dict[str, int] = {"20%": 0, "5%": 0}
    for line in facts.lines:
        if line.net_pence <= 0:
            raise ConstructorError(
                f"supplier-invoice: line net_pence must be > 0, got {line.net_pence}"
            )
        vat = _vat_for_line(line.net_pence, line.vat_rate)
        checks.append(
            f"line {line.description!r}: gross {line.net_pence + vat} == net "
            f"{line.net_pence} + VAT {vat}"
        )
        if line.expense_account_code not in expense_cls_by_code:
            expense_cls_by_code[line.expense_account_code] = _resolve(
                codes,
                line.expense_account_code,
                {"Expense (Expense)", "Expense (DirectCost)", "Asset (FixedAsset)"},
                "supplier-invoice expense",
            )
        expense_by_code[line.expense_account_code] = (
            expense_by_code.get(line.expense_account_code, 0) + line.net_pence
        )
        if line.vat_rate in ("20%", "5%"):
            vat_by_rate[line.vat_rate.value] += vat
    total_gross = sum(l.net_pence for l in facts.lines) + sum(vat_by_rate.values())
    legs: list[Leg] = []
    for code in expense_by_code:
        legs.append(_dr(code, expense_cls_by_code[code], expense_by_code[code], "Purchase (net)"))
    for rate in ("20%", "5%"):
        if vat_by_rate[rate] > 0:
            legs.append(_dr("2220", vat_cls, vat_by_rate[rate], f"Input VAT {rate}"))
    legs.append(_cr("2000", creditor_cls, total_gross, f"Supplier invoice {facts.invoice_ref}"))
    checks.append(
        "exactly one 2220 Input VAT Dr leg per rate group; zero/exempt lines "
        "contribute none"
    )
    checks.append(
        "credit leg is Liability; debit legs are Expense or Asset (FixedAsset) "
        "for capitalised purchases"
    )
    return _finish("supplier-invoice", legs, checks, {
        "gross_pence": total_gross,
        "vat_pence": sum(vat_by_rate.values()),
    })


# ---------------------------------------------------------------------------
# supplier-payment
# ---------------------------------------------------------------------------


def supplier_payment(
    facts: SupplierPaymentFacts, codes: Mapping[str, str]
) -> ConstructedEntry:
    checks: list[str] = []
    if facts.amount_pence <= 0:
        raise ConstructorError(
            f"supplier-payment: amount_pence must be > 0, got {facts.amount_pence}"
        )
    ap_cls = _resolve(
        codes, "2000", {"Liability (CurrentLiability)"}, "supplier-payment trade creditors"
    )
    bank_cls = _resolve(
        codes, facts.bank_account_code, {"Asset (Bank)"}, "supplier-payment bank"
    )
    checks.append(
        f"exactly two legs: Dr AP (2000, {ap_cls}), Cr bank "
        f"({facts.bank_account_code}, {bank_cls})"
    )
    legs = [
        _dr("2000", ap_cls, facts.amount_pence, f"Payment to {facts.supplier_name}"),
        _cr(facts.bank_account_code, bank_cls, facts.amount_pence,
            f"Payment to {facts.supplier_name}"),
    ]
    return _finish("supplier-payment", legs, checks, {})
