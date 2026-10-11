"""Pydantic facts models for the F4 constructors — one per catalog skill.

Field names, types and requiredness mirror each SKILL.md ``## Facts`` table
1:1. Money is ALWAYS integer pence. ``vat_rate`` uses the engine's
``VALID_VAT_RATES`` values via :class:`VatRate`.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Literal, Mapping

from pydantic import BaseModel, Field, ValidationError, model_validator

__all__ = [
    "VatRate",
    "QueryFacts",
    "QUERY_TYPES",
    "BankTransferFacts",
    "CustomerReceiptFacts",
    "DepreciationFacts",
    "JournalAccrualFacts",
    "OpeningBalanceItem",
    "OpeningBalancesFacts",
    "PayrollFacts",
    "PrepaymentFacts",
    "SalesInvoiceLineFacts",
    "SalesInvoiceFacts",
    "SupplierInvoiceLineFacts",
    "SupplierInvoiceFacts",
    "SupplierPaymentFacts",
    "FACTS_MODELS",
    "parse_facts",
]


class VatRate(str, Enum):
    """Engine VAT rates (``VALID_VAT_RATES``)."""

    STANDARD = "20%"
    REDUCED = "5%"
    ZERO = "0%"
    EXEMPT = "exempt"


class BankTransferFacts(BaseModel):
    source_account_code: str
    destination_account_code: str
    amount_pence: int
    transfer_date: date
    reference: str | None = None


class CustomerReceiptFacts(BaseModel):
    customer_name: str
    receipt_date: date
    bank_account_code: str
    amount_pence: int
    invoice_refs: list[str] = Field(default_factory=list)
    allocated_pence: list[int] = Field(default_factory=list)


class DepreciationFacts(BaseModel):
    asset_account_code: str
    cost_pence: int
    residual_pence: int = 0
    useful_life_months: int
    period_end_date: date
    method: Literal["straight_line"]


class JournalAccrualFacts(BaseModel):
    expense_account_code: str
    amount_pence: int
    period_end_date: date
    description: str
    reversal_date: date | None = None
    is_provision: bool = False


class OpeningBalanceItem(BaseModel):
    code: str
    name: str
    balance_pence: int


class OpeningBalancesFacts(BaseModel):
    opening_date: date
    assets: list[OpeningBalanceItem]
    liabilities: list[OpeningBalanceItem]
    equity_account_code: str


class PayrollFacts(BaseModel):
    period_end_date: date
    pay_date: date
    gross_pence: int
    paye_pence: int
    employee_nic_pence: int
    employer_nic_pence: int
    pension_employee_pence: int = 0
    pension_employer_pence: int = 0
    pension_payable_account_code: str | None = None
    bank_account_code: str

    @model_validator(mode="after")
    def _pension_payable_code_required(self) -> "PayrollFacts":
        if (self.pension_employee_pence or self.pension_employer_pence) and not (
            self.pension_payable_account_code
        ):
            raise ValueError(
                "pension_payable_account_code is required whenever a pension "
                "field is set (the client must create it in their COA)"
            )
        return self


class PrepaymentFacts(BaseModel):
    direction: Literal["prepaid_expense", "deferred_income"]
    account_code: str
    total_pence: int
    start_date: date
    end_date: date
    release_schedule: Literal["monthly", "per_period"]
    bank_account_code: str
    vat_rate: VatRate | None = None


class SalesInvoiceLineFacts(BaseModel):
    description: str
    net_pence: int
    vat_rate: VatRate
    revenue_account_code: str


class SalesInvoiceFacts(BaseModel):
    customer_name: str
    invoice_ref: str
    invoice_date: date
    due_date: date | None = None
    lines: list[SalesInvoiceLineFacts]


class SupplierInvoiceLineFacts(BaseModel):
    description: str
    net_pence: int
    vat_rate: VatRate
    expense_account_code: str


class SupplierInvoiceFacts(BaseModel):
    supplier_name: str
    invoice_ref: str
    invoice_date: date
    due_date: date | None = None
    lines: list[SupplierInvoiceLineFacts]


class SupplierPaymentFacts(BaseModel):
    supplier_name: str
    payment_date: date
    bank_account_code: str
    amount_pence: int
    invoice_refs: list[str] = Field(default_factory=list)
    allocated_pence: list[int] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Contract F29 — query mode (NOT a catalog skill)
# ---------------------------------------------------------------------------

#: The deterministic, read-only question types the orchestrator answers.
QUERY_TYPES: frozenset[str] = frozenset(
    {"trial_balance", "account_balance", "accounts", "contacts", "pending"}
)


class QueryFacts(BaseModel):
    """Contract F29 query-mode facts — classify a QUESTION about the books.

    ``query_type`` is deliberately a plain ``str`` (not a ``Literal``) and
    is validated against :data:`QUERY_TYPES` by the orchestrator: an
    unknown type must surface as the structured ``invalid_query`` error,
    not as a facts-validation error.  ``account_code`` is only meaningful
    for ``account_balance``.  No money ever travels through this model —
    the orchestrator answers from the reports/COA/contacts services in
    integer pence.
    """

    query_type: str
    account_code: str | None = None


FACTS_MODELS: dict[str, type[BaseModel]] = {
    "bank-transfer": BankTransferFacts,
    "customer-receipt": CustomerReceiptFacts,
    "depreciation": DepreciationFacts,
    "journal-accrual": JournalAccrualFacts,
    "opening-balances": OpeningBalancesFacts,
    "payroll": PayrollFacts,
    "prepayment": PrepaymentFacts,
    "sales-invoice": SalesInvoiceFacts,
    "supplier-invoice": SupplierInvoiceFacts,
    "supplier-payment": SupplierPaymentFacts,
}


def parse_facts(skill_id: str, data: Mapping[str, Any]) -> BaseModel:
    """Validate raw fact data into the skill's facts model.

    Raises :class:`ConstructorError` (never pydantic's ``ValidationError``)
    for unknown skills, missing required facts, or invalid values (e.g. an
    unknown ``vat_rate``), so every facts failure surfaces as the single
    exception type of the constructors package.
    """
    from . import ConstructorError  # deferred import — avoids an import cycle

    model_cls = FACTS_MODELS.get(skill_id)
    if model_cls is None:
        raise ConstructorError(f"unknown skill id {skill_id!r}")
    try:
        return model_cls.model_validate(dict(data))
    except ValidationError as exc:
        raise ConstructorError(f"invalid facts for {skill_id}: {exc}") from exc
