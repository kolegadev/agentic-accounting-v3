"""Initial-ledger setup — entity → COA → bank accounts → ledger → opening drafts.

Contract F11/F12: creates the initial ledger for a new accounting entity.

Order of operations:
  1. Refuse only when the entity's ``setup_state`` is already ``complete``
     (idempotency = refusal for finished setups, resume for partial ones).
  2. Mark ``setup_state='in_progress'`` and COMMIT before any heavy step so
     a mid-setup failure persists the state and a re-run RESUMEES instead of
     being refused.
  3. Load the COA template via ``CoaService.load_template`` (fuzzy match) —
     skipped when accounts already exist (resume).
  4. Create bank accounts via ``BankService`` — skipped when any bank
     account already exists (resume).
  5. Ensure the Formance ledger exists via ``formance_client.create_ledger``
     (tolerated; ALWAYS attempted — idempotent on the Formance side).
  6. Build the F4 ``opening-balances`` constructor facts from the request,
     run the REAL constructor, resolve codes via ``CoaService``, and create a
     DRAFT transaction via ``TransactionService.create_transaction`` — never
     posted here.  Skipped when a draft transaction already exists (resume).
     Approval happens through the orchestrator approve route.

On success ``setup_state='complete'`` is committed.  Partial data is never
deleted on failure.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Optional, Union

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.constructors import CONSTRUCTORS, ConstructorError, resolve_from_coa
from src.constructors.facts import OpeningBalanceItem, OpeningBalancesFacts
from src.models.account import Account
from src.models.bank_account import BankAccount
from src.models.transaction import Transaction
from src.services.bank_service import BankService
from src.services.coa_service import CoaService
from src.services.entity_service import EntityNotFoundError, EntityService
from src.services.formance_client import formance_client
from src.services.ledger_parity import verify_parity
from src.services.transaction_service import TransactionService
from src.validators.bank import BankAccountCreate
from src.validators.entity import (
    EntityResponse,
    EntityStatus,
    SetupBankAccount,
    SetupOpeningBalanceItem,
    SetupOpeningBalances,
    SetupResult,
)
from src.validators.transaction import PostingCreate, TransactionCreate

logger = logging.getLogger(__name__)


class SetupError(Exception):
    """Base exception for setup errors (defaults to 409 conflict)."""

    def __init__(self, message: str, status_code: int = 409) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


async def _account_count(db: AsyncSession) -> int:
    """Number of rows in the global chart of accounts."""
    result = await db.execute(select(func.count()).select_from(Account))
    return int(result.scalar_one_or_none() or 0)


async def _bank_count(db: AsyncSession) -> int:
    """Number of bank account rows (any row, active or not)."""
    result = await db.execute(select(func.count()).select_from(BankAccount))
    return int(result.scalar_one_or_none() or 0)


async def _draft_count(db: AsyncSession) -> int:
    """Number of draft transactions."""
    result = await db.execute(
        select(func.count())
        .select_from(Transaction)
        .where(Transaction.status == "draft")
    )
    return int(result.scalar_one_or_none() or 0)


async def _create_opening_draft(
    db: AsyncSession,
    ob: SetupOpeningBalances,
) -> Transaction:
    """Build the F4 opening-balances facts, run the constructor, create a draft."""
    # All COA accounts -> the constructor's code -> canonical class resolver.
    result = await db.execute(select(Account))
    orm_accounts: list[Account] = list(result.scalars().all())
    codes = resolve_from_coa(orm_accounts)

    by_code = {a.code: a for a in orm_accounts}

    def to_item(item: SetupOpeningBalanceItem) -> OpeningBalanceItem:
        name = item.name
        if not name:
            acct = by_code.get(item.code)
            name = acct.name if acct is not None else item.code
        return OpeningBalanceItem(
            code=item.code,
            name=name,
            balance_pence=item.balance_pence,
        )

    facts = OpeningBalancesFacts(
        opening_date=ob.opening_date,
        assets=[to_item(i) for i in ob.assets],
        liabilities=[to_item(i) for i in ob.liabilities],
        equity_account_code=ob.equity_account_code,
    )

    try:
        entry = CONSTRUCTORS["opening-balances"](facts, codes)
    except ConstructorError as exc:
        raise SetupError(str(exc), status_code=422) from exc

    postings: list[PostingCreate] = []
    for leg in entry.legs:
        account = await CoaService.get_account_by_code(db, leg.account)
        if account is None:
            raise SetupError(
                f"Account code '{leg.account}' not found in the COA",
                status_code=404,
            )
        postings.append(
            PostingCreate(
                account_id=account.id,
                debit_amount=leg.debit_amount,
                credit_amount=leg.credit_amount,
                description=leg.description,
            )
        )

    tx_data = TransactionCreate(
        description=f"Opening balances as at {ob.opening_date.isoformat()}",
        effective_date=ob.opening_date,
        idempotency_key=uuid.uuid4(),
        postings=postings,
    )
    # Draft only — never posted here. Approval is the orchestrator's job.
    transaction = await TransactionService.create_transaction(db, tx_data)
    return transaction


async def setup_entity(
    db: AsyncSession,
    entity_id: uuid.UUID,
    template_name: str,
    bank_accounts: Optional[list[SetupBankAccount]] = None,
    opening_balances: Optional[
        Union[SetupOpeningBalances, list[SetupOpeningBalances]]
    ] = None,
) -> SetupResult:
    """Set up the initial ledger for an entity — resumable after failure.

    Raises EntityNotFoundError (404) for unknown entities and SetupError
    (409) when the entity is already fully set up.  On any mid-setup failure
    the state remains ``in_progress``: a re-run resumes (COA / bank /
    opening-balance steps skip cleanly) instead of being refused.  Partial
    data is never deleted.
    """
    entity = await EntityService.get_entity(db, entity_id)
    if entity is None:
        raise EntityNotFoundError(entity_id)

    # GUARD — refuse only a COMPLETED setup; partial setups resume.
    if entity.setup_state == "complete":
        raise SetupError(f"Entity '{entity_id}' is already set up")

    # Mark in-progress and COMMIT before any heavy step so a failure below
    # leaves 'in_progress' persisted (the resume entry point).
    if entity.setup_state != "in_progress":
        entity.setup_state = "in_progress"
        await db.commit()

    # 2. Chart of accounts (fuzzy *_vat / *_no_vat match inside CoaService).
    #    Skipped on resume when accounts already exist.
    coa_accounts: list = []
    if await _account_count(db) == 0:
        coa_accounts = await CoaService.load_template(db, template_name)

    # 3. Bank accounts — skipped on resume when any bank account exists.
    bank_account_ids: list[uuid.UUID] = []
    if await _bank_count(db) == 0:
        for ba in bank_accounts or []:
            created = await BankService.create_account(
                db,
                BankAccountCreate(
                    name=ba.name,
                    sort_code=ba.sort_code,
                    account_number=ba.account_number,
                    opening_balance=ba.opening_balance,
                ),
            )
            bank_account_ids.append(created.id)

    # 4. Formance ledger — ALWAYS attempted (idempotent on Formance's side);
    #    tolerated; Formance may be started later.
    await formance_client.create_ledger(entity.ledger_name)

    # 5. Opening balances -> F4 constructor -> DRAFT transaction(s).
    #    Skipped on resume when a draft transaction already exists.
    opening_ids: list[uuid.UUID] = []
    draft_total = 0
    if opening_balances is not None and await _draft_count(db) == 0:
        sets = (
            opening_balances
            if isinstance(opening_balances, list)
            else [opening_balances]
        )
        for ob in sets:
            transaction = await _create_opening_draft(db, ob)
            opening_ids.append(transaction.id)
            draft_total += sum(p.debit_amount for p in transaction.postings)

    entity.setup_state = "complete"
    await db.commit()

    return SetupResult(
        entity_id=entity_id,
        coa_count=len(coa_accounts),
        bank_account_ids=bank_account_ids,
        ledger_name=entity.ledger_name,
        opening_transaction_ids=opening_ids,
        opening_draft_total_pence=draft_total,
    )


async def get_entity_status(db: AsyncSession, entity_id: uuid.UUID) -> EntityStatus:
    """Status snapshot for an entity: COA, bank accounts, drafts, ledger, parity.

    Raises EntityNotFoundError (404) for unknown entities.
    """
    entity = await EntityService.get_entity(db, entity_id)
    if entity is None:
        raise EntityNotFoundError(entity_id)

    account_count = await _account_count(db)
    bank_accounts = await BankService.list_accounts(db)
    pending, _total = await TransactionService.list_transactions(
        db, status="draft", limit=500
    )
    pending_responses = [
        TransactionService._transaction_to_response(t) for t in pending
    ]

    if not os.getenv("FORNANCE_LEDGER_URL"):
        formance_status, parity = "not_configured", "na"
    else:
        reachable = await formance_client.health_check()
        if not reachable:
            formance_status, parity = "unreachable", "na"
        else:
            formance_status = "reachable"
            report = await verify_parity(db, ledger=entity.ledger_name)
            parity = "ok" if report.ok else "drift"

    return EntityStatus(
        entity=EntityResponse.model_validate(entity),
        has_coa=account_count > 0,
        account_count=account_count,
        bank_accounts=bank_accounts,
        pending_opening_transactions=pending_responses,
        ledger_name=entity.ledger_name,
        formance=formance_status,
        parity=parity,
    )
