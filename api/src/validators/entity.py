"""Pydantic schemas for Entity + initial-ledger setup (Contract F11)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field

from src.validators.bank import BankAccountResponse
from src.validators.transaction import TransactionResponse


class EntityCreate(BaseModel):
    """Schema for creating a new accounting entity."""

    name: str = Field(..., min_length=1, max_length=255)
    legal_type: str = Field(default="limited_company")
    framework: str = Field(default="uk-gaap")
    fiscal_year_start: str = Field(default="04-06", max_length=5)
    vat_registered: bool = False
    vat_number: Optional[str] = Field(default=None, max_length=20)
    vat_scheme: Optional[str] = Field(default=None, max_length=10)
    currency: str = Field(default="GBP", min_length=3, max_length=3)
    ledger_name: Optional[str] = Field(default=None, max_length=64)


class EntityUpdate(BaseModel):
    """Schema for partially updating an entity."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    legal_type: Optional[str] = None
    framework: Optional[str] = None
    fiscal_year_start: Optional[str] = Field(default=None, max_length=5)
    vat_registered: Optional[bool] = None
    vat_number: Optional[str] = Field(default=None, max_length=20)
    vat_scheme: Optional[str] = Field(default=None, max_length=10)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)
    is_active: Optional[bool] = None


class EntityResponse(BaseModel):
    """Schema for entity responses (all fields)."""

    id: uuid.UUID
    name: str
    legal_type: str
    framework: str
    fiscal_year_start: str
    vat_registered: bool
    vat_number: Optional[str] = None
    vat_scheme: Optional[str] = None
    currency: str
    ledger_name: str
    created_by: uuid.UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SetupBankAccount(BaseModel):
    """A bank account to create during initial-ledger setup."""

    name: str = Field(..., min_length=1, max_length=255)
    sort_code: Optional[str] = Field(default=None, max_length=10)
    account_number: Optional[str] = Field(default=None, max_length=20)
    opening_balance: int = Field(default=0, description="Opening balance in pence")


class SetupOpeningBalanceItem(BaseModel):
    """One opening-balance line: an account code and its brought-forward balance."""

    code: str = Field(..., description="Account code from the loaded COA")
    balance_pence: int = Field(..., description="Balance in pence (positive)")
    name: Optional[str] = Field(
        default=None, description="Account name; defaults to the COA name"
    )


class SetupOpeningBalances(BaseModel):
    """Opening-balance facts for one F4 opening-balances entry."""

    opening_date: date
    equity_account_code: str
    assets: list[SetupOpeningBalanceItem] = Field(default_factory=list)
    liabilities: list[SetupOpeningBalanceItem] = Field(default_factory=list)


class SetupRequest(BaseModel):
    """Body for POST /entities/{id}/setup (or the `setup` field on create)."""

    template_name: str = Field(..., min_length=1)
    bank_accounts: Optional[list[SetupBankAccount]] = None
    opening_balances: Optional[SetupOpeningBalances | list[SetupOpeningBalances]] = None


class EntityCreateWithSetup(EntityCreate):
    """POST /entities body: entity fields plus optional immediate setup."""

    setup: Optional[SetupRequest] = None


class SetupResult(BaseModel):
    """Outcome of setup_entity."""

    entity_id: uuid.UUID
    coa_count: int
    bank_account_ids: list[uuid.UUID]
    ledger_name: str
    opening_transaction_ids: list[uuid.UUID]
    opening_draft_total_pence: int


class EntitySetupResponse(BaseModel):
    """POST /entities response: the entity plus optional setup result."""

    entity: EntityResponse
    setup: Optional[SetupResult] = None


class EntityStatus(BaseModel):
    """GET /entities/{id}/status response."""

    entity: EntityResponse
    has_coa: bool
    account_count: int
    bank_accounts: list[BankAccountResponse]
    pending_opening_transactions: list[TransactionResponse]
    ledger_name: str
    formance: str
    parity: str
