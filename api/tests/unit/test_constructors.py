"""Contract F4 tests — deterministic entry constructors.

(a) Binding: every SKILL.md worked example (parsed from its ``dr-cr`` block
    and its Formance interaction fence) is reproduced EXACTLY by the
    constructor for that skill — codes, directions, pence amounts, and
    Formance postings. Any future SKILL.md edit that changes an example
    without changing the constructor fails CI.
(b) Property: 200 randomized valid fact sets per skill (deterministic RNG,
    seed 42) always balance, obey exact VAT arithmetic, reference only
    resolvable classes, and produce Formance postings consistent with the
    legs.
(c) Errors: unknown vat_rate, unknown account class, zero amounts, missing
    required facts, and unrepresentable facts raise ConstructorError.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import FrozenInstanceError
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.constructors import (
    CONSTRUCTORS,
    ConstructorError,
    ConstructedEntry,
    Leg,
    resolve_from_coa,
)
from src.constructors.facts import parse_facts

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_DIR = REPO_ROOT / "skills" / "catalog" / "transactions"
VAT_TEMPLATE = REPO_ROOT / "api" / "src" / "coa_templates" / "uk_limited_company_vat.json"

DRCR_FENCE_RE = re.compile(r"^```dr-cr\s*$(.*?)^```\s*$", re.MULTILINE | re.DOTALL)
JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)
AMOUNT_RE = re.compile(r"^\d+\.\d{2}$")

BANK_CODES = ["1000", "1010"]
EXPENSE_CODES = ["6000", "6010", "6040", "6050", "6300", "6310"]
FIXED_ASSET_CODES = ["1300", "1310", "1320", "1330", "1340", "1350"]
SALES_RATE_CODES = {"20%": "4000", "5%": "4010", "0%": "4020", "exempt": "4030"}
SUPPLIER_RATE_CODES = {
    "20%": ["5010", "5020", "5100", "6000", "6010"],
    "5%": ["5110", "6100"],
    "0%": ["6020"],
    "exempt": ["6030", "6400", "6410"],
}


# ---------------------------------------------------------------------------
# Catalog parsing helpers (mirror tests/test_skill_catalog.py)
# ---------------------------------------------------------------------------


def _read_skill(skill_id: str) -> str:
    return (SKILLS_DIR / skill_id / "SKILL.md").read_text(encoding="utf-8")


def _parse_drcr_blocks(text: str) -> list[list[tuple[str, str, int]]]:
    blocks: list[list[tuple[str, str, int]]] = []
    for match in DRCR_FENCE_RE.finditer(text):
        legs: list[tuple[str, str, int]] = []
        for line in match.group(1).splitlines():
            line = line.strip()
            if not line:
                continue
            tokens = line.split()
            assert tokens[0] in ("Dr", "Cr"), f"bad dr-cr leg: {line!r}"
            assert re.fullmatch(r"\d{4}", tokens[1]), f"bad account code: {line!r}"
            legs.append((tokens[0], tokens[1], int(tokens[-1])))
        assert legs, "empty dr-cr fenced block"
        blocks.append(legs)
    return blocks


def _formance_fences(text: str) -> list[list[dict]]:
    section_match = re.search(
        r"^## Formance interaction\s*$(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert section_match is not None, "missing '## Formance interaction' section"
    fences: list[list[dict]] = []
    for match in JSON_FENCE_RE.finditer(section_match.group(1)):
        parsed = json.loads(match.group(1))
        if isinstance(parsed, dict):
            parsed = [parsed]
        assert isinstance(parsed, list), "Formance block must be a list or dict"
        fences.append(parsed)
    return fences


def _template_codes(**extra: str) -> dict[str, str]:
    """Code -> canonical class resolver from the UK VAT COA template."""
    accounts = json.loads(VAT_TEMPLATE.read_text(encoding="utf-8"))
    codes = resolve_from_coa(accounts)
    codes.update(extra)
    return codes


# ---------------------------------------------------------------------------
# Worked-example fixtures (facts mirror each SKILL.md worked example prose)
# ---------------------------------------------------------------------------

_SALARY_FACTS = {
    "period_end_date": "2026-06-30",
    "pay_date": "2026-06-30",
    "gross_pence": 250000,
    "paye_pence": 22000,
    "employee_nic_pence": 13000,
    "employer_nic_pence": 15000,
    "bank_account_code": "1000",
}

_PENSION_FACTS = {
    **_SALARY_FACTS,
    "pension_employee_pence": 5000,
    "pension_employer_pence": 7500,
    "pension_payable_account_code": "2041",
}

_PREPAID_FACTS = {
    "direction": "prepaid_expense",
    "account_code": "6030",
    "total_pence": 120000,
    "start_date": "2026-01-01",
    "end_date": "2026-12-31",
    "release_schedule": "monthly",
    "bank_account_code": "1000",
}

_DEFERRED_FACTS = {
    **_PREPAID_FACTS,
    "direction": "deferred_income",
    "account_code": "4030",
}

WORKED_CASES: dict[str, list[dict]] = {
    "bank-transfer": [
        {
            "facts": {
                "source_account_code": "1000",
                "destination_account_code": "1010",
                "amount_pence": 50000,
                "transfer_date": "2026-01-15",
                "reference": "SWEEP-001",
            },
            "kwargs": {},
        },
    ],
    "customer-receipt": [
        {
            "facts": {
                "customer_name": "Acme Ltd",
                "receipt_date": "2026-01-20",
                "bank_account_code": "1000",
                "amount_pence": 60000,
                "invoice_refs": ["INV-1001"],
            },
            "kwargs": {},
        },
    ],
    "depreciation": [
        {
            "facts": {
                "asset_account_code": "1320",
                "cost_pence": 300000,
                "residual_pence": 0,
                "useful_life_months": 60,
                "period_end_date": "2026-01-31",
                "method": "straight_line",
            },
            "kwargs": {},
        },
    ],
    "journal-accrual": [
        {
            "facts": {
                "expense_account_code": "6000",
                "amount_pence": 120000,
                "period_end_date": "2026-12-31",
                "description": "December rent",
            },
            "kwargs": {},
        },
        {
            "facts": {
                "expense_account_code": "6000",
                "amount_pence": 120000,
                "period_end_date": "2026-12-31",
                "description": "December rent",
                "reversal_date": "2027-01-01",
            },
            "kwargs": {"reversal": True},
        },
    ],
    "opening-balances": [
        {
            "facts": {
                "opening_date": "2026-01-01",
                "assets": [
                    {"code": "1000", "name": "Bank Current Account", "balance_pence": 150000},
                    {"code": "1300", "name": "Land & Buildings", "balance_pence": 50000},
                    {"code": "1100", "name": "Trade Debtors", "balance_pence": 25000},
                ],
                "liabilities": [
                    {"code": "2000", "name": "Trade Creditors", "balance_pence": 30000},
                    {"code": "2210", "name": "Output VAT", "balance_pence": 5000},
                    {"code": "2100", "name": "Bank Loan", "balance_pence": 40000},
                ],
                "equity_account_code": "3000",
            },
            "kwargs": {},
        },
    ],
    "payroll": [
        {"facts": _SALARY_FACTS, "kwargs": {"journal": "salary"}},
        {"facts": _SALARY_FACTS, "kwargs": {"journal": "employer_ni"}},
        {"facts": _PENSION_FACTS, "kwargs": {"journal": "salary"}},
        {"facts": _PENSION_FACTS, "kwargs": {"journal": "employer_pension"}},
    ],
    "prepayment": [
        {"facts": _PREPAID_FACTS, "kwargs": {"stage": "initial"}},
        {"facts": _PREPAID_FACTS, "kwargs": {"stage": "release", "period_index": 0}},
        {"facts": _DEFERRED_FACTS, "kwargs": {"stage": "initial"}},
        {"facts": _DEFERRED_FACTS, "kwargs": {"stage": "release", "period_index": 0}},
    ],
    "sales-invoice": [
        {
            "facts": {
                "customer_name": "Acme Ltd",
                "invoice_ref": "INV-1001",
                "invoice_date": "2026-01-10",
                "lines": [
                    {
                        "description": "Consulting services",
                        "net_pence": 50000,
                        "vat_rate": "20%",
                        "revenue_account_code": "4000",
                    },
                ],
            },
            "kwargs": {},
        },
        {
            "facts": {
                "customer_name": "Acme Ltd",
                "invoice_ref": "INV-1002",
                "invoice_date": "2026-01-10",
                "lines": [
                    {
                        "description": "Exempt sale",
                        "net_pence": 25000,
                        "vat_rate": "exempt",
                        "revenue_account_code": "4030",
                    },
                ],
            },
            "kwargs": {},
        },
    ],
    "supplier-invoice": [
        {
            "facts": {
                "supplier_name": "Materials Co",
                "invoice_ref": "S-2026-001",
                "invoice_date": "2026-01-10",
                "lines": [
                    {
                        "description": "Raw materials",
                        "net_pence": 50000,
                        "vat_rate": "20%",
                        "expense_account_code": "5010",
                    },
                ],
            },
            "kwargs": {},
        },
        {
            "facts": {
                "supplier_name": "Insurance Co",
                "invoice_ref": "S-2026-002",
                "invoice_date": "2026-01-10",
                "lines": [
                    {
                        "description": "Insurance",
                        "net_pence": 25000,
                        "vat_rate": "exempt",
                        "expense_account_code": "6030",
                    },
                ],
            },
            "kwargs": {},
        },
    ],
    "supplier-payment": [
        {
            "facts": {
                "supplier_name": "Materials Co",
                "payment_date": "2026-01-25",
                "bank_account_code": "1000",
                "amount_pence": 60000,
                "invoice_refs": ["S-2026-001"],
            },
            "kwargs": {},
        },
    ],
}

WORKED_EXTRA_CODES: dict[str, dict[str, str]] = {
    "payroll": {"2041": "Liability (CurrentLiability)"},
}

WORKED_PARAMS = [
    (skill_id, index)
    for skill_id, cases in WORKED_CASES.items()
    for index in range(len(cases))
]


# ---------------------------------------------------------------------------
# (a) Worked-example binding tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill_id,case_index", WORKED_PARAMS)
def test_worked_example_binds_constructor(skill_id: str, case_index: int) -> None:
    """The constructor reproduces its SKILL.md worked example EXACTLY."""
    text = _read_skill(skill_id)
    blocks = _parse_drcr_blocks(text)
    fences = _formance_fences(text)
    cases = WORKED_CASES[skill_id]
    assert len(blocks) == len(fences) == len(cases), (
        f"{skill_id}: {len(blocks)} dr-cr blocks, {len(fences)} fences and "
        f"{len(cases)} test cases must line up"
    )
    block = blocks[case_index]
    fence = fences[case_index]
    case = cases[case_index]

    facts = parse_facts(skill_id, case["facts"])
    codes = _template_codes(**WORKED_EXTRA_CODES.get(skill_id, {}))
    entry = CONSTRUCTORS[skill_id](facts, codes, **case["kwargs"])

    assert isinstance(entry, ConstructedEntry)
    assert entry.skill_id == skill_id

    expected = [(direction, code, pence) for direction, code, pence in block]
    actual: list[tuple[str, str, int]] = []
    for leg in entry.legs:
        assert (leg.debit_amount > 0) != (leg.credit_amount > 0), (
            f"{skill_id}: leg {leg} must have exactly one non-zero side"
        )
        if leg.debit_amount:
            actual.append(("Dr", leg.account, leg.debit_amount))
        else:
            actual.append(("Cr", leg.account, leg.credit_amount))
    assert actual == expected, (
        f"{skill_id} case {case_index}: legs {actual} do not match worked "
        f"example {expected}"
    )

    assert entry.formance_postings == fence, (
        f"{skill_id} case {case_index}: Formance postings "
        f"{entry.formance_postings} do not match fence {fence}"
    )

    for leg in entry.legs:
        assert codes[leg.account] == leg.account_class, (
            f"{skill_id}: leg {leg.account} class {leg.account_class} != "
            f"resolver class {codes[leg.account]}"
        )


# ---------------------------------------------------------------------------
# (b) Property tests — 200 randomized valid facts per skill
# ---------------------------------------------------------------------------


def _check_common_invariants(
    entry: ConstructedEntry, codes: dict[str, str], skill_id: str
) -> None:
    """Invariants every constructed entry must satisfy."""
    debits = sum(leg.debit_amount for leg in entry.legs)
    credits = sum(leg.credit_amount for leg in entry.legs)
    assert debits == credits > 0, (
        f"{skill_id}: unbalanced — debits {debits}, credits {credits}"
    )
    for leg in entry.legs:
        assert (leg.debit_amount > 0) != (leg.credit_amount > 0), (
            f"{skill_id}: leg {leg} must have exactly one non-zero side"
        )
        assert leg.account in codes, f"{skill_id}: unresolved account {leg.account}"
        assert leg.account_class == codes[leg.account], (
            f"{skill_id}: leg {leg.account} class {leg.account_class} != "
            f"{codes[leg.account]}"
        )
    postings = entry.formance_postings
    assert postings, f"{skill_id}: no Formance postings"
    total = Decimal(0)
    for posting in postings:
        assert set(posting) == {"source", "destination", "amount", "asset"}
        assert posting["source"] in codes, f"{skill_id}: source {posting['source']}"
        assert posting["destination"] in codes
        assert posting["source"] != "world" and posting["destination"] != "world"
        assert posting["asset"] == "GBP/2"
        assert AMOUNT_RE.fullmatch(posting["amount"])
        total += Decimal(posting["amount"])
    assert total * 100 == debits, (
        f"{skill_id}: posting total {total} != debits {debits}"
    )


def _vat_for_line(net: int, rate: str) -> int:
    """Test-side mirror of the skill's per-line VAT arithmetic."""
    if rate == "20%":
        return net // 5
    if rate == "5%":
        return net // 20
    return 0


def _gross_split(total: int, rate: str | None) -> tuple[int, int]:
    """Test-side mirror of the constructor's VAT gross-up."""
    if rate == "20%":
        vat = total // 6
        return total - vat, vat
    if rate == "5%":
        vat = total // 21
        return total - vat, vat
    return total, 0


def _end_of_month(start: date, months: int) -> date:
    year, month = start.year, start.month + months - 1
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def test_property_bank_transfer() -> None:
    rng = random.Random(42)
    codes = _template_codes()
    for _ in range(200):
        source, destination = rng.sample(BANK_CODES, 2)
        facts = parse_facts("bank-transfer", {
            "source_account_code": source,
            "destination_account_code": destination,
            "amount_pence": rng.randint(1, 10_000_000),
            "transfer_date": date(2026, rng.randint(1, 12), rng.randint(1, 28)),
        })
        entry = CONSTRUCTORS["bank-transfer"](facts, codes)
        _check_common_invariants(entry, codes, "bank-transfer")
        assert len(entry.legs) == 2
        assert all(leg.account_class == "Asset (Bank)" for leg in entry.legs)
        assert len(entry.formance_postings) == 1
        assert entry.legs[0].debit_amount == facts.amount_pence
        assert entry.legs[1].credit_amount == facts.amount_pence


def test_property_customer_receipt() -> None:
    rng = random.Random(42)
    codes = _template_codes()
    for _ in range(200):
        facts = parse_facts("customer-receipt", {
            "customer_name": f"Customer {rng.randint(1, 9999)}",
            "receipt_date": date(2026, rng.randint(1, 12), rng.randint(1, 28)),
            "bank_account_code": rng.choice(BANK_CODES),
            "amount_pence": rng.randint(1, 10_000_000),
        })
        entry = CONSTRUCTORS["customer-receipt"](facts, codes)
        _check_common_invariants(entry, codes, "customer-receipt")
        dr, cr = entry.legs[0], entry.legs[1]
        assert dr.account == facts.bank_account_code
        assert dr.account_class == "Asset (Bank)"
        assert cr.account == "1100"
        assert cr.account_class == "Asset (CurrentAsset)"
        assert dr.debit_amount == cr.credit_amount == facts.amount_pence


def test_property_depreciation() -> None:
    rng = random.Random(42)
    codes = _template_codes()
    for _ in range(200):
        cost = rng.randint(1000, 100_000_000)
        life = rng.randint(1, 60)
        residual = rng.randint(0, max(0, cost - life))
        facts = parse_facts("depreciation", {
            "asset_account_code": rng.choice(FIXED_ASSET_CODES),
            "cost_pence": cost,
            "residual_pence": residual,
            "useful_life_months": life,
            "period_end_date": date(2026, rng.randint(1, 12), 28),
            "method": "straight_line",
        })
        entry = CONSTRUCTORS["depreciation"](facts, codes)
        _check_common_invariants(entry, codes, "depreciation")
        charge = (2 * (cost - residual) + life) // (2 * life)
        assert charge > 0
        assert entry.derived["monthly_charge_pence"] == charge
        dr, cr = entry.legs[0], entry.legs[1]
        assert dr.account == "6500" and dr.account_class == "Expense (Expense)"
        assert cr.account == "1360" and cr.account_class == "Asset (FixedAsset)"
        assert dr.debit_amount == cr.credit_amount == charge
        assert charge <= cost - residual


def test_property_journal_accrual() -> None:
    rng = random.Random(42)
    codes = _template_codes()
    for _ in range(200):
        facts = parse_facts("journal-accrual", {
            "expense_account_code": rng.choice(EXPENSE_CODES),
            "amount_pence": rng.randint(1, 10_000_000),
            "period_end_date": date(2026, rng.randint(1, 12), 28),
            "description": f"Accrual {rng.randint(1, 9999)}",
        })
        entry = CONSTRUCTORS["journal-accrual"](facts, codes)
        _check_common_invariants(entry, codes, "journal-accrual")
        assert len(entry.legs) == 2
        assert entry.legs[0].account == facts.expense_account_code
        assert entry.legs[0].account_class in ("Expense (Expense)", "Expense (DirectCost)")
        assert entry.legs[1].account == "2010"
        assert entry.legs[1].account_class == "Liability (CurrentLiability)"
        reversal = CONSTRUCTORS["journal-accrual"](facts, codes, reversal=True)
        _check_common_invariants(reversal, codes, "journal-accrual reversal")
        assert reversal.legs[0].account == "2010"
        assert reversal.legs[0].debit_amount == facts.amount_pence
        assert reversal.legs[1].account == facts.expense_account_code
        assert reversal.legs[1].credit_amount == facts.amount_pence


def test_property_opening_balances() -> None:
    rng = random.Random(42)
    codes = _template_codes()
    asset_pool = ["1000", "1010", "1100", "1110", "1200", "1210", *FIXED_ASSET_CODES]
    liability_pool = ["2000", "2010", "2020", "2030", "2040", "2100", "2110", "2200", "2210"]
    for _ in range(200):
        assets = [
            {"code": code, "name": code, "balance_pence": rng.randint(1, 10_000_000)}
            for code in rng.sample(asset_pool, rng.randint(1, 4))
        ]
        liabilities = [
            {"code": code, "name": code, "balance_pence": rng.randint(1, 10_000_000)}
            for code in rng.sample(liability_pool, rng.randint(1, 4))
        ]
        equity_code = rng.choice(["3000", "3020"])
        facts = parse_facts("opening-balances", {
            "opening_date": "2026-01-01",
            "assets": assets,
            "liabilities": liabilities,
            "equity_account_code": equity_code,
        })
        entry = CONSTRUCTORS["opening-balances"](facts, codes)
        _check_common_invariants(entry, codes, "opening-balances")
        total_assets = sum(a["balance_pence"] for a in assets)
        total_liabilities = sum(li["balance_pence"] for li in liabilities)
        equity = total_assets - total_liabilities
        assert entry.derived["equity_pence"] == equity
        assert len(entry.legs) == (
            len(assets) + len(liabilities) + (1 if equity != 0 else 0)
        )
        for leg in entry.legs[: len(assets)]:
            assert leg.debit_amount > 0
            assert leg.account_class.startswith("Asset ")
        for leg in entry.legs[len(assets): len(assets) + len(liabilities)]:
            assert leg.credit_amount > 0
            assert leg.account_class.startswith("Liability ")
        if equity != 0:
            eq_leg = entry.legs[-1]
            assert eq_leg.account == equity_code
            assert eq_leg.account_class.startswith("Equity ")
            if equity > 0:
                assert eq_leg.credit_amount == equity
            else:
                assert eq_leg.debit_amount == -equity


def test_property_payroll() -> None:
    rng = random.Random(42)
    codes = _template_codes()
    codes["2041"] = "Liability (CurrentLiability)"
    for _ in range(200):
        gross = rng.randint(10_000, 5_000_000)
        paye = rng.randint(0, gross // 3)
        employee_nic = rng.randint(0, gross // 4)
        employee_pension = (
            rng.randint(0, gross - paye - employee_nic)
            if rng.random() < 0.5
            else 0
        )
        employer_nic = rng.randint(1, 200_000)
        employer_pension = rng.randint(1, 200_000) if rng.random() < 0.5 else 0
        facts_dict: dict = {
            "period_end_date": "2026-06-30",
            "pay_date": "2026-06-30",
            "gross_pence": gross,
            "paye_pence": paye,
            "employee_nic_pence": employee_nic,
            "employer_nic_pence": employer_nic,
            "bank_account_code": rng.choice(BANK_CODES),
        }
        if employee_pension or employer_pension:
            facts_dict["pension_employee_pence"] = employee_pension
            facts_dict["pension_employer_pence"] = employer_pension
            facts_dict["pension_payable_account_code"] = "2041"
        facts = parse_facts("payroll", facts_dict)
        net = gross - paye - employee_nic - employee_pension
        entry = CONSTRUCTORS["payroll"](facts, codes, journal="salary")
        _check_common_invariants(entry, codes, "payroll salary")
        assert entry.derived["net_pence"] == net
        assert entry.legs[0].account == "6600"
        assert entry.legs[0].debit_amount == gross
        cr_map: dict[str, int] = {}
        for leg in entry.legs[1:]:
            cr_map[leg.account] = cr_map.get(leg.account, 0) + leg.credit_amount
        assert cr_map.get(facts.bank_account_code, 0) == net
        assert cr_map.get("2030", 0) == paye + employee_nic
        assert cr_map.get("2041", 0) == employee_pension
        assert sum(cr_map.values()) == gross
        ni_entry = CONSTRUCTORS["payroll"](facts, codes, journal="employer_ni")
        _check_common_invariants(ni_entry, codes, "payroll employer_ni")
        assert ni_entry.legs[0].account == "6610"
        assert ni_entry.legs[1].account == "2030"
        assert ni_entry.legs[0].debit_amount == ni_entry.legs[1].credit_amount == employer_nic
        if employer_pension:
            pen_entry = CONSTRUCTORS["payroll"](facts, codes, journal="employer_pension")
            _check_common_invariants(pen_entry, codes, "payroll employer_pension")
            assert pen_entry.legs[0].account == "6630"
            assert pen_entry.legs[1].account == "2041"
            assert (
                pen_entry.legs[0].debit_amount
                == pen_entry.legs[1].credit_amount
                == employer_pension
            )


def test_property_prepayment() -> None:
    rng = random.Random(42)
    codes = _template_codes()
    prepaid_codes = ["6000", "6010", "6030", "6040", "6050"]
    deferred_codes = ["4000", "4020", "4030"]
    for _ in range(200):
        direction = rng.choice(["prepaid_expense", "deferred_income"])
        account_code = rng.choice(
            prepaid_codes if direction == "prepaid_expense" else deferred_codes
        )
        schedule = rng.choice(["monthly", "per_period"])
        vat_rate = rng.choice([None, None, None, "20%", "5%", "0%", "exempt"])
        start = date(2026, rng.randint(1, 6), 1)
        n_months = rng.randint(1, 24)
        # Generate the NET amount first for rated invoices so the gross
        # (net + vat) round-trips exactly through the constructor's
        # gross-up — for an arbitrary gross, net + vat == gross and
        # vat == net // 5 cannot both hold (gross % 6 == 5 is impossible).
        net_target = rng.randint(1000, 10_000_000)
        vat_target = (
            net_target // 5 if vat_rate == "20%" else net_target // 20
            if vat_rate == "5%" else 0
        )
        total = net_target + vat_target
        facts = parse_facts("prepayment", {
            "direction": direction,
            "account_code": account_code,
            "total_pence": total,
            "start_date": start.isoformat(),
            "end_date": _end_of_month(start, n_months).isoformat(),
            "release_schedule": schedule,
            "bank_account_code": rng.choice(BANK_CODES),
            "vat_rate": vat_rate,
        })
        net, vat = _gross_split(total, vat_rate)
        periods = n_months if schedule == "monthly" else 1
        init = CONSTRUCTORS["prepayment"](facts, codes, stage="initial")
        _check_common_invariants(init, codes, "prepayment initial")
        assert init.derived["net_pence"] == net
        assert init.derived["vat_pence"] == vat
        assert init.derived["gross_pence"] == total
        bank_leg = next(li for li in init.legs if li.account == facts.bank_account_code)
        if direction == "prepaid_expense":
            prepay_leg = next(li for li in init.legs if li.account == "1110")
            assert prepay_leg.debit_amount == net
            assert bank_leg.credit_amount == total
            if vat:
                vat_leg = next(li for li in init.legs if li.account == "2220")
                assert vat_leg.debit_amount == vat
        else:
            deferred_leg = next(li for li in init.legs if li.account == "2020")
            assert deferred_leg.credit_amount == net
            assert bank_leg.debit_amount == total
            if vat:
                vat_leg = next(li for li in init.legs if li.account == "2210")
                assert vat_leg.credit_amount == vat
        if vat:
            expected_vat = net // 5 if vat_rate == "20%" else net // 20
            assert vat == expected_vat
        released = 0
        for index in range(periods):
            rel = CONSTRUCTORS["prepayment"](
                facts, codes, stage="release", period_index=index
            )
            _check_common_invariants(rel, codes, "prepayment release")
            released += sum(leg.debit_amount for leg in rel.legs)
        assert released == net


def test_property_sales_invoice() -> None:
    rng = random.Random(42)
    codes = _template_codes()
    for _ in range(200):
        lines = [
            {
                "description": f"Line {rng.randint(1, 9999)}",
                "net_pence": rng.randint(1, 5_000_000),
                "vat_rate": rate,
                "revenue_account_code": SALES_RATE_CODES[rate],
            }
            for rate in (rng.choice(list(SALES_RATE_CODES)) for _ in range(rng.randint(1, 3)))
        ]
        facts = parse_facts("sales-invoice", {
            "customer_name": "Acme Ltd",
            "invoice_ref": f"INV-{rng.randint(1, 99999)}",
            "invoice_date": "2026-01-10",
            "lines": lines,
        })
        entry = CONSTRUCTORS["sales-invoice"](facts, codes)
        _check_common_invariants(entry, codes, "sales-invoice")
        vat_by_rate = {"20%": 0, "5%": 0}
        expected_gross = 0
        for line in lines:
            vat = _vat_for_line(line["net_pence"], line["vat_rate"])
            expected_gross += line["net_pence"] + vat
            if line["vat_rate"] in vat_by_rate:
                vat_by_rate[line["vat_rate"]] += vat
        expected_vat = sum(vat_by_rate.values())
        assert entry.derived["gross_pence"] == expected_gross
        assert entry.derived["vat_pence"] == expected_vat
        dr = next(leg for leg in entry.legs if leg.debit_amount)
        assert dr.account == "1100" and dr.debit_amount == expected_gross
        credits = [leg for leg in entry.legs if leg.credit_amount]
        revenue_sum = sum(leg.credit_amount for leg in credits if leg.account != "2210")
        vat_sum = sum(leg.credit_amount for leg in credits if leg.account == "2210")
        assert revenue_sum == expected_gross - expected_vat
        assert vat_sum == expected_vat
        vat_legs = [leg for leg in credits if leg.account == "2210"]
        assert len(vat_legs) == len([v for v in vat_by_rate.values() if v > 0])


def test_property_supplier_invoice() -> None:
    rng = random.Random(42)
    codes = _template_codes()
    for _ in range(200):
        lines = []
        for _ in range(rng.randint(1, 3)):
            rate = rng.choice(list(SUPPLIER_RATE_CODES))
            code = rng.choice(SUPPLIER_RATE_CODES[rate])
            if rng.random() < 0.2:
                code = rng.choice(FIXED_ASSET_CODES)
            lines.append({
                "description": f"Line {rng.randint(1, 9999)}",
                "net_pence": rng.randint(1, 5_000_000),
                "vat_rate": rate,
                "expense_account_code": code,
            })
        facts = parse_facts("supplier-invoice", {
            "supplier_name": "Materials Co",
            "invoice_ref": f"S-{rng.randint(1, 99999)}",
            "invoice_date": "2026-01-10",
            "lines": lines,
        })
        entry = CONSTRUCTORS["supplier-invoice"](facts, codes)
        _check_common_invariants(entry, codes, "supplier-invoice")
        vat_by_rate = {"20%": 0, "5%": 0}
        expected_gross = 0
        for line in lines:
            vat = _vat_for_line(line["net_pence"], line["vat_rate"])
            expected_gross += line["net_pence"] + vat
            if line["vat_rate"] in vat_by_rate:
                vat_by_rate[line["vat_rate"]] += vat
        expected_vat = sum(vat_by_rate.values())
        assert entry.derived["gross_pence"] == expected_gross
        assert entry.derived["vat_pence"] == expected_vat
        credits = [leg for leg in entry.legs if leg.credit_amount]
        assert len(credits) == 1
        assert credits[0].account == "2000"
        assert credits[0].account_class == "Liability (CurrentLiability)"
        assert credits[0].credit_amount == expected_gross
        for leg in entry.legs:
            if leg.debit_amount:
                assert leg.account_class in (
                    "Expense (Expense)",
                    "Expense (DirectCost)",
                    "Asset (FixedAsset)",
                    "Liability (CurrentLiability)",  # 2220 Input VAT contra-liability
                )
        vat_legs = [leg for leg in entry.legs if leg.account == "2220"]
        assert len(vat_legs) == len([v for v in vat_by_rate.values() if v > 0])
        assert sum(leg.debit_amount for leg in vat_legs) == expected_vat


def test_property_supplier_payment() -> None:
    rng = random.Random(42)
    codes = _template_codes()
    for _ in range(200):
        facts = parse_facts("supplier-payment", {
            "supplier_name": f"Supplier {rng.randint(1, 9999)}",
            "payment_date": date(2026, rng.randint(1, 12), rng.randint(1, 28)),
            "bank_account_code": rng.choice(BANK_CODES),
            "amount_pence": rng.randint(1, 10_000_000),
        })
        entry = CONSTRUCTORS["supplier-payment"](facts, codes)
        _check_common_invariants(entry, codes, "supplier-payment")
        dr, cr = entry.legs[0], entry.legs[1]
        assert dr.account == "2000"
        assert dr.account_class == "Liability (CurrentLiability)"
        assert cr.account == facts.bank_account_code
        assert cr.account_class == "Asset (Bank)"
        assert dr.debit_amount == cr.credit_amount == facts.amount_pence


# ---------------------------------------------------------------------------
# (c) Error tests — every failure surfaces as ConstructorError
# ---------------------------------------------------------------------------


def test_error_unknown_vat_rate_raises_constructor_error() -> None:
    with pytest.raises(ConstructorError):
        parse_facts("sales-invoice", {
            "customer_name": "Acme",
            "invoice_ref": "I-1",
            "invoice_date": "2026-01-01",
            "lines": [{
                "description": "d",
                "net_pence": 1000,
                "vat_rate": "25%",
                "revenue_account_code": "4000",
            }],
        })


def test_error_missing_required_fact_raises_constructor_error() -> None:
    with pytest.raises(ConstructorError):
        parse_facts("bank-transfer", {
            "source_account_code": "1000",
            "destination_account_code": "1010",
            "transfer_date": "2026-01-01",
        })


def test_error_unknown_skill_id() -> None:
    with pytest.raises(ConstructorError):
        parse_facts("not-a-skill", {})


def test_error_unknown_account_class() -> None:
    codes = _template_codes()
    facts = parse_facts("customer-receipt", {
        "customer_name": "Acme",
        "receipt_date": "2026-01-01",
        "bank_account_code": "9999",
        "amount_pence": 1000,
    })
    with pytest.raises(ConstructorError):
        CONSTRUCTORS["customer-receipt"](facts, codes)


def test_error_unknown_canonical_account_code() -> None:
    codes = {code: cls for code, cls in _template_codes().items() if code != "2030"}
    facts = parse_facts("payroll", {
        "period_end_date": "2026-06-30",
        "pay_date": "2026-06-30",
        "gross_pence": 250000,
        "paye_pence": 22000,
        "employee_nic_pence": 13000,
        "employer_nic_pence": 15000,
        "bank_account_code": "1000",
    })
    with pytest.raises(ConstructorError):
        CONSTRUCTORS["payroll"](facts, codes, journal="employer_ni")


def test_error_zero_amounts() -> None:
    codes = _template_codes()
    cases = [
        (
            "bank-transfer",
            {
                "source_account_code": "1000",
                "destination_account_code": "1010",
                "amount_pence": 0,
                "transfer_date": "2026-01-01",
            },
            {},
        ),
        (
            "customer-receipt",
            {
                "customer_name": "Acme",
                "receipt_date": "2026-01-01",
                "bank_account_code": "1000",
                "amount_pence": 0,
            },
            {},
        ),
        (
            "journal-accrual",
            {
                "expense_account_code": "6000",
                "amount_pence": 0,
                "period_end_date": "2026-01-31",
                "description": "d",
            },
            {},
        ),
        (
            "supplier-payment",
            {
                "supplier_name": "Acme",
                "payment_date": "2026-01-01",
                "bank_account_code": "1000",
                "amount_pence": 0,
            },
            {},
        ),
        (
            "payroll",
            {
                "period_end_date": "2026-06-30",
                "pay_date": "2026-06-30",
                "gross_pence": 250000,
                "paye_pence": 22000,
                "employee_nic_pence": 13000,
                "employer_nic_pence": 0,
                "bank_account_code": "1000",
            },
            {"journal": "employer_ni"},
        ),
        (
            "prepayment",
            {
                "direction": "prepaid_expense",
                "account_code": "6030",
                "total_pence": 0,
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
                "release_schedule": "monthly",
                "bank_account_code": "1000",
            },
            {},
        ),
        (
            "depreciation",
            {
                "asset_account_code": "1320",
                "cost_pence": 100000,
                "residual_pence": 100000,
                "useful_life_months": 60,
                "period_end_date": "2026-01-31",
                "method": "straight_line",
            },
            {},
        ),
    ]
    for skill_id, facts_dict, kwargs in cases:
        facts = parse_facts(skill_id, facts_dict)
        with pytest.raises(ConstructorError):
            CONSTRUCTORS[skill_id](facts, codes, **kwargs)


def test_error_same_account_transfer() -> None:
    facts = parse_facts("bank-transfer", {
        "source_account_code": "1000",
        "destination_account_code": "1000",
        "amount_pence": 100,
        "transfer_date": "2026-01-01",
    })
    with pytest.raises(ConstructorError):
        CONSTRUCTORS["bank-transfer"](facts, _template_codes())


def test_error_payroll_net_negative() -> None:
    facts = parse_facts("payroll", {
        "period_end_date": "2026-06-30",
        "pay_date": "2026-06-30",
        "gross_pence": 10000,
        "paye_pence": 9000,
        "employee_nic_pence": 9000,
        "employer_nic_pence": 1000,
        "bank_account_code": "1000",
    })
    with pytest.raises(ConstructorError):
        CONSTRUCTORS["payroll"](facts, _template_codes())


def test_error_pension_without_payable_account() -> None:
    with pytest.raises(ConstructorError):
        parse_facts("payroll", {
            "period_end_date": "2026-06-30",
            "pay_date": "2026-06-30",
            "gross_pence": 250000,
            "paye_pence": 22000,
            "employee_nic_pence": 13000,
            "employer_nic_pence": 15000,
            "pension_employee_pence": 5000,
            "bank_account_code": "1000",
        })


def test_error_depreciation_contra_as_asset() -> None:
    facts = parse_facts("depreciation", {
        "asset_account_code": "1360",
        "cost_pence": 300000,
        "residual_pence": 0,
        "useful_life_months": 60,
        "period_end_date": "2026-01-31",
        "method": "straight_line",
    })
    with pytest.raises(ConstructorError):
        CONSTRUCTORS["depreciation"](facts, _template_codes())


def test_error_wrong_class_for_canonical_code() -> None:
    codes = _template_codes()
    codes["2210"] = "Asset (Bank)"
    facts = parse_facts("sales-invoice", {
        "customer_name": "Acme",
        "invoice_ref": "I-1",
        "invoice_date": "2026-01-01",
        "lines": [{
            "description": "d",
            "net_pence": 50000,
            "vat_rate": "20%",
            "revenue_account_code": "4000",
        }],
    })
    with pytest.raises(ConstructorError):
        CONSTRUCTORS["sales-invoice"](facts, codes)


def test_error_prepayment_period_out_of_range() -> None:
    facts = parse_facts("prepayment", _PREPAID_FACTS)
    with pytest.raises(ConstructorError):
        CONSTRUCTORS["prepayment"](
            facts, _template_codes(), stage="release", period_index=12
        )


# ---------------------------------------------------------------------------
# Registry / resolver / types
# ---------------------------------------------------------------------------


def test_registry_covers_every_catalog_skill() -> None:
    skill_ids = {path.parent.name for path in SKILLS_DIR.glob("*/SKILL.md")}
    assert set(CONSTRUCTORS) == skill_ids
    assert len(CONSTRUCTORS) == 10
    for skill_id, constructor in CONSTRUCTORS.items():
        assert callable(constructor), f"{skill_id} constructor is not callable"


def test_resolve_from_coa_builds_canonical_classes() -> None:
    accounts = json.loads(VAT_TEMPLATE.read_text(encoding="utf-8"))
    codes = resolve_from_coa(accounts)
    assert codes["1000"] == "Asset (Bank)"
    assert codes["1100"] == "Asset (CurrentAsset)"
    assert codes["1360"] == "Asset (FixedAsset)"
    assert codes["2000"] == "Liability (CurrentLiability)"
    assert codes["2100"] == "Liability (LongTermLiability)"
    assert codes["3000"] == "Equity (Equity)"
    assert codes["4000"] == "Revenue (Revenue)"
    assert codes["6600"] == "Expense (Expense)"
    assert codes["5010"] == "Expense (DirectCost)"


def test_resolve_from_coa_accepts_dict_and_object_accounts() -> None:
    accounts = json.loads(VAT_TEMPLATE.read_text(encoding="utf-8"))
    dict_mapping = resolve_from_coa(accounts)
    object_mapping = resolve_from_coa(
        [SimpleNamespace(**account) for account in accounts[:5]]
    )
    for account in accounts[:5]:
        assert object_mapping[account["code"]] == dict_mapping[account["code"]]


def test_entry_types_are_frozen_and_error_is_exception() -> None:
    assert issubclass(ConstructorError, Exception)
    leg = Leg(
        account="1000",
        account_class="Asset (Bank)",
        debit_amount=1,
        credit_amount=0,
        description="x",
    )
    with pytest.raises(FrozenInstanceError):
        leg.debit_amount = 2  # type: ignore[misc]
    entry = ConstructedEntry(
        skill_id="x", legs=[], formance_postings=[], derived={}, checks=[]
    )
    with pytest.raises(FrozenInstanceError):
        entry.derived = {}  # type: ignore[misc]
