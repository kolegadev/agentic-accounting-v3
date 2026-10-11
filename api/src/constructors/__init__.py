"""Contract F4 — deterministic entry constructors (facts -> DR/CR legs).

One constructor per transaction-catalog skill (see ``CONSTRUCTORS``): each
turns a pydantic facts model (``src.constructors.facts``) into a balanced
``ConstructedEntry``. Constructors are pure — account resolution goes
through a ``codes`` mapping of account CODE -> canonical class string
(``"Asset (Bank)"``, ``"Expense (Expense)"``, ...), built from a company's
real chart of accounts by :func:`resolve_from_coa` using the canonical
``"{category} ({type})"`` classes the COA templates define. A code that is
missing from the resolver, or that has the wrong class, raises
``ConstructorError`` — never an unbalanced entry.

The ``formance_postings`` of a ``ConstructedEntry`` are produced by the
REAL ``src.services.transaction_service.build_formance_postings`` (F1)
with deterministic UUIDs keyed by account code — never reimplemented.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "ConstructorError",
    "Leg",
    "ConstructedEntry",
    "canonical_class",
    "resolve_from_coa",
    "CONSTRUCTORS",
]


class ConstructorError(Exception):
    """Raised when facts cannot be represented as a balanced entry."""


@dataclass(frozen=True)
class Leg:
    """One side of a journal entry.

    Exactly one of ``debit_amount`` / ``credit_amount`` is non-zero.
    """

    account: str          # account CODE resolved from the company COA
    account_class: str    # canonical class from the skill's DR/CR table
    debit_amount: int     # pence, 0 for credit legs
    credit_amount: int    # pence, 0 for debit legs
    description: str


@dataclass(frozen=True)
class ConstructedEntry:
    skill_id: str
    legs: list[Leg]                     # exactly one of debit/credit > 0 per leg
    formance_postings: list[dict]       # via build_formance_postings (F1)
    derived: dict[str, int]             # vat_pence, gross_pence, ... derived values
    checks: list[str]                   # verification checks executed + results


def canonical_class(category: str, account_type: str) -> str:
    """Canonical account class string: ``"{category} ({type})"``."""
    return f"{category} ({account_type})"


def _account_attr(account: Any, name: str) -> Any:
    """Read ``name`` from an ORM account object or a plain dict."""
    try:
        return getattr(account, name)
    except AttributeError:
        return account[name]


def resolve_from_coa(coa_accounts: Any) -> dict[str, str]:
    """Build the constructor ``codes`` resolver from COA account objects.

    Maps every account CODE to its canonical class string (``"Asset (Bank)"``,
    ``"Liability (CurrentLiability)"``, ...) using the category/type scheme the
    COA templates define. Accepts ORM account objects (attributes) or plain
    dicts with ``code`` / ``category`` / ``type`` keys.

    The mapping is keyed by code (not class) so that classes with several
    accounts — e.g. two bank accounts, three payroll expense accounts — stay
    representable; constructors validate that each fact-carried or
    skill-canonical code exists here with the class the skill requires.
    """
    codes: dict[str, str] = {}
    for account in coa_accounts:
        code = _account_attr(account, "code")
        category = _account_attr(account, "category")
        account_type = _account_attr(account, "type")
        codes[str(code)] = canonical_class(str(category), str(account_type))
    return codes


from . import entry_constructors as _entry  # noqa: E402  (import after definitions)

CONSTRUCTORS: dict[str, Any] = {
    "bank-transfer": _entry.bank_transfer,
    "card-purchase": _entry.card_purchase,
    "customer-receipt": _entry.customer_receipt,
    "depreciation": _entry.depreciation,
    "journal-accrual": _entry.journal_accrual,
    "opening-balances": _entry.opening_balances,
    "payroll": _entry.payroll,
    "prepayment": _entry.prepayment,
    "sales-invoice": _entry.sales_invoice,
    "supplier-invoice": _entry.supplier_invoice,
    "supplier-payment": _entry.supplier_payment,
}
