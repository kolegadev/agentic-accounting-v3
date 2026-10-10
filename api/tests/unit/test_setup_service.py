"""Unit tests for SetupService (Contract F11/F12) with mocked DB/Formance.

F12 semantics: ``setup_state`` lifecycle (none → in_progress → complete).
Refusal only for COMPLETE setups; partial setups resume with clean step
skips (COA when accounts exist, banks when a bank exists, opening balances
when a draft exists).  ``create_ledger`` is always attempted.
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.models.account import Account
from src.models.entity import Entity
from src.services.entity_service import EntityNotFoundError
from src.services.formance_client import formance_client
from src.services.setup_service import SetupError, setup_entity
from src.validators.account import AccountResponse
from src.validators.bank import BankAccountResponse
from src.validators.entity import (
    SetupBankAccount,
    SetupOpeningBalanceItem,
    SetupOpeningBalances,
)
from tests.conftest import NOW

ENTITY_ID = uuid.uuid4()
TX_ID = uuid.uuid4()
BANK_ACCOUNT_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db() -> AsyncMock:
    """AsyncMock DB session."""
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    return db


def make_entity(**overrides) -> Entity:
    kwargs = {
        "id": ENTITY_ID,
        "name": "Acme Holdings Ltd",
        "legal_type": "limited_company",
        "framework": "uk-gaap",
        "fiscal_year_start": "04-06",
        "vat_registered": True,
        "vat_scheme": "standard",
        "currency": "GBP",
        "ledger_name": "entity-acme123",
        "created_by": uuid.uuid4(),
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    kwargs.update(overrides)
    return Entity(**kwargs)


def make_account_response(code: str, name: str, category: str, type_: str) -> AccountResponse:
    return AccountResponse(
        id=uuid.uuid4(),
        code=code,
        name=name,
        category=category,
        type=type_,
        vat_rate=None,
        parent_id=None,
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )


def make_orm_account(code: str, name: str, category: str, type_: str) -> Account:
    return Account(
        id=uuid.uuid4(),
        code=code,
        name=name,
        category=category,
        type=type_,
        vat_rate=None,
        parent_id=None,
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )


def count_result(value: int) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def accounts_result(accounts: list[Account]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = accounts
    return result


async def _fake_create_transaction(db, data):
    tx = MagicMock()
    tx.id = TX_ID
    tx.postings = data.postings
    return tx


@pytest.fixture
def setup_patches(mock_db: AsyncMock):
    """Patch the collaborators setup_entity calls.

    DB execute calls in order: account count (COA skip check), bank count
    (bank skip check), draft count (opening skip check), then the
    all-accounts query inside ``_create_opening_draft``.
    """
    bank_1000 = make_orm_account("1000", "Bank Current Account", "Asset", "Bank")
    equity_3000 = make_orm_account("3000", "Share Capital", "Equity", "Equity")

    call_index = {"n": 0}
    counts = {"accounts": 0, "banks": 0, "drafts": 0}

    def _execute(stmt, *args, **kwargs):
        call_index["n"] += 1
        if call_index["n"] <= 3:
            values = [counts["accounts"], counts["banks"], counts["drafts"]]
            return count_result(values[call_index["n"] - 1])
        return accounts_result([bank_1000, equity_3000])

    mock_db.execute.side_effect = _execute

    entity = make_entity()
    coa_responses = [
        make_account_response("1000", "Bank Current Account", "Asset", "Bank"),
        make_account_response("3000", "Share Capital", "Equity", "Equity"),
    ]
    by_code = {a.code: a for a in coa_responses}

    mock_ledger = AsyncMock(return_value=True)
    mock_create_tx = AsyncMock(side_effect=_fake_create_transaction)

    async def _get_by_code(db, code):
        return by_code.get(code)

    with (
        patch(
            "src.services.setup_service.EntityService.get_entity",
            AsyncMock(return_value=entity),
        ) as mock_get_entity,
        patch(
            "src.services.setup_service.CoaService.load_template",
            AsyncMock(return_value=coa_responses),
        ) as mock_load_template,
        patch(
            "src.services.setup_service.BankService.create_account",
            AsyncMock(
                return_value=BankAccountResponse(
                    id=BANK_ACCOUNT_ID,
                    name="Barclays Business Current",
                    sort_code="20-00-00",
                    account_number="12345678",
                    iban=None,
                    currency="GBP",
                    opening_balance=0,
                    current_balance=0,
                    is_active=True,
                    created_at=NOW,
                    updated_at=NOW,
                )
            ),
        ) as mock_create_bank,
        patch.object(formance_client, "create_ledger", mock_ledger),
        patch(
            "src.services.setup_service.CoaService.get_account_by_code",
            AsyncMock(side_effect=_get_by_code),
        ),
        patch(
            "src.services.setup_service.TransactionService.create_transaction",
            mock_create_tx,
        ),
    ):
        yield {
            "get_entity": mock_get_entity,
            "load_template": mock_load_template,
            "create_bank": mock_create_bank,
            "ledger": mock_ledger,
            "create_tx": mock_create_tx,
            "coa_responses": coa_responses,
            "counts": counts,
            "entity": entity,
        }


OPENING = SetupOpeningBalances(
    opening_date=date(2026, 4, 6),
    equity_account_code="3000",
    assets=[SetupOpeningBalanceItem(code="1000", balance_pence=1000000)],
)


# ---------------------------------------------------------------------------
# Guard / errors
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_setup_unknown_entity_404(mock_db: AsyncMock) -> None:
    """Unknown entity raises EntityNotFoundError (404)."""
    with patch(
        "src.services.setup_service.EntityService.get_entity",
        AsyncMock(return_value=None),
    ):
        with pytest.raises(EntityNotFoundError):
            await setup_entity(mock_db, uuid.uuid4(), "uk_limited_company_vat")


@pytest.mark.asyncio
async def test_setup_refuses_only_when_state_complete(
    setup_patches, mock_db: AsyncMock
) -> None:
    """A COMPLETE entity refuses (409) before touching the COA."""
    setup_patches["entity"].setup_state = "complete"

    with pytest.raises(SetupError, match="already set up") as excinfo:
        await setup_entity(mock_db, ENTITY_ID, "uk_limited_company_vat")

    assert excinfo.value.status_code == 409
    setup_patches["load_template"].assert_not_awaited()
    setup_patches["ledger"].assert_not_awaited()


@pytest.mark.asyncio
async def test_setup_resume_skips_coa_when_accounts_exist(
    setup_patches, mock_db: AsyncMock
) -> None:
    """Accounts already exist + state not complete → COA load SKIPPED, setup
    continues (banks/ledger still run) and finishes complete."""
    setup_patches["counts"]["accounts"] = 65

    result = await setup_entity(
        mock_db,
        ENTITY_ID,
        "uk_limited_company_vat",
        bank_accounts=[SetupBankAccount(name="Barclays", opening_balance=1000)],
    )

    setup_patches["load_template"].assert_not_awaited()
    assert result.coa_count == 0
    assert result.bank_account_ids == [BANK_ACCOUNT_ID]
    assert setup_patches["entity"].setup_state == "complete"


@pytest.mark.asyncio
async def test_double_setup_refused(setup_patches, mock_db: AsyncMock) -> None:
    """First setup succeeds (state → complete); a second run refuses."""
    result = await setup_entity(
        mock_db,
        ENTITY_ID,
        "uk_limited_company_vat",
        bank_accounts=[SetupBankAccount(name="Barclays", opening_balance=1000)],
        opening_balances=OPENING,
    )
    assert result.coa_count == 2
    assert setup_patches["entity"].setup_state == "complete"

    with pytest.raises(SetupError, match="already set up"):
        await setup_entity(mock_db, ENTITY_ID, "uk_limited_company_vat")

    assert setup_patches["load_template"].await_count == 1


# ---------------------------------------------------------------------------
# Resumability — step skips and state transitions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resume_skips_banks_when_bank_exists(
    setup_patches, mock_db: AsyncMock
) -> None:
    """A bank account already exists → create_account NOT called again."""
    setup_patches["counts"]["banks"] = 1

    result = await setup_entity(
        mock_db,
        ENTITY_ID,
        "uk_limited_company_vat",
        bank_accounts=[SetupBankAccount(name="Barclays", opening_balance=1000)],
    )

    setup_patches["create_bank"].assert_not_awaited()
    assert result.bank_account_ids == []
    assert setup_patches["entity"].setup_state == "complete"


@pytest.mark.asyncio
async def test_resume_skips_opening_when_draft_exists(
    setup_patches, mock_db: AsyncMock
) -> None:
    """A draft transaction already exists → opening balances NOT recreated."""
    setup_patches["counts"]["drafts"] = 1

    result = await setup_entity(
        mock_db, ENTITY_ID, "uk_limited_company_vat", opening_balances=OPENING
    )

    setup_patches["create_tx"].assert_not_awaited()
    assert result.opening_transaction_ids == []
    assert result.opening_draft_total_pence == 0
    assert setup_patches["entity"].setup_state == "complete"


@pytest.mark.asyncio
async def test_create_ledger_always_attempted_even_on_full_resume(
    setup_patches, mock_db: AsyncMock
) -> None:
    """create_ledger is idempotent and ALWAYS called — even when every
    other step is skipped on resume."""
    setup_patches["counts"]["accounts"] = 65
    setup_patches["counts"]["banks"] = 1
    setup_patches["counts"]["drafts"] = 1
    setup_patches["entity"].setup_state = "in_progress"

    result = await setup_entity(
        mock_db,
        ENTITY_ID,
        "uk_limited_company_vat",
        bank_accounts=[SetupBankAccount(name="Barclays", opening_balance=1000)],
        opening_balances=OPENING,
    )

    setup_patches["ledger"].assert_awaited_once_with("entity-acme123")
    setup_patches["load_template"].assert_not_awaited()
    setup_patches["create_bank"].assert_not_awaited()
    setup_patches["create_tx"].assert_not_awaited()
    assert result.coa_count == 0
    assert setup_patches["entity"].setup_state == "complete"


@pytest.mark.asyncio
async def test_in_progress_state_committed_before_heavy_steps(
    mock_db: AsyncMock,
) -> None:
    """The in_progress transition is COMMITTED first — a failure in a later
    step leaves in_progress persisted (resume, not refusal)."""
    entity = make_entity()
    mock_db.execute.side_effect = [count_result(0)]

    with patch(
        "src.services.setup_service.EntityService.get_entity",
        AsyncMock(return_value=entity),
    ):
        with patch(
            "src.services.setup_service.CoaService.load_template",
            AsyncMock(side_effect=RuntimeError("template load failed")),
        ):
            with pytest.raises(RuntimeError, match="template load failed"):
                await setup_entity(mock_db, ENTITY_ID, "uk_limited_company_vat")

    assert entity.setup_state == "in_progress"
    assert mock_db.commit.await_count >= 1


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_setup_creates_ledger_and_bank(setup_patches, mock_db: AsyncMock) -> None:
    """create_ledger is called with the entity's ledger name; bank created."""
    result = await setup_entity(
        mock_db,
        ENTITY_ID,
        "uk_limited_company_vat",
        bank_accounts=[
            SetupBankAccount(
                name="Barclays Business Current",
                sort_code="20-00-00",
                account_number="12345678",
                opening_balance=1000,
            )
        ],
    )

    setup_patches["ledger"].assert_awaited_once_with("entity-acme123")
    assert setup_patches["create_bank"].await_count == 1
    assert result.entity_id == ENTITY_ID
    assert result.ledger_name == "entity-acme123"
    assert result.bank_account_ids == [BANK_ACCOUNT_ID]
    assert result.opening_transaction_ids == []
    assert result.opening_draft_total_pence == 0


@pytest.mark.asyncio
async def test_opening_balance_facts_produce_balanced_draft(
    setup_patches, mock_db: AsyncMock
) -> None:
    """Opening balances map into exact F4 legs: Dr 1000 1,000,000 / Cr 3000 1,000,000."""
    result = await setup_entity(
        mock_db, ENTITY_ID, "uk_limited_company_vat", opening_balances=OPENING
    )

    assert result.opening_transaction_ids == [TX_ID]
    assert result.opening_draft_total_pence == 1000000

    captured = setup_patches["create_tx"].await_args.args[1]
    assert captured.description == "Opening balances as at 2026-04-06"
    assert captured.effective_date == date(2026, 4, 6)

    bank = setup_patches["coa_responses"][0]  # code 1000
    equity = setup_patches["coa_responses"][1]  # code 3000
    legs = {
        (p.account_id, p.debit_amount, p.credit_amount) for p in captured.postings
    }
    assert legs == {
        (bank.id, 1000000, 0),
        (equity.id, 0, 1000000),
    }
    assert sum(p.debit_amount for p in captured.postings) == 1000000
    assert sum(p.credit_amount for p in captured.postings) == 1000000

    # Draft only — posting is never attempted by setup.
    bank_1000 = make_orm_account("1000", "Bank Current Account", "Asset", "Bank")
    equity_3000 = make_orm_account("3000", "Share Capital", "Equity", "Equity")
    with patch(
        "src.services.setup_service.TransactionService.post_transaction",
        AsyncMock(),
    ) as mock_post:
        setup_patches["entity"].setup_state = "none"  # fresh lifecycle
        mock_db.execute.side_effect = [
            count_result(0),
            count_result(0),
            count_result(0),
            accounts_result([bank_1000, equity_3000]),
        ]
        await setup_entity(
            mock_db, ENTITY_ID, "uk_limited_company_vat", opening_balances=OPENING
        )
        mock_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_ledger_failure_tolerated(setup_patches, mock_db: AsyncMock) -> None:
    """create_ledger returning False must not fail the setup."""
    setup_patches["ledger"].return_value = False

    result = await setup_entity(
        mock_db, ENTITY_ID, "uk_limited_company_vat", opening_balances=OPENING
    )
    assert result.entity_id == ENTITY_ID
    assert result.opening_transaction_ids == [TX_ID]


@pytest.mark.asyncio
async def test_opening_balance_wrong_class_rejected(
    setup_patches, mock_db: AsyncMock
) -> None:
    """An asset code that is not an Asset class must surface as a SetupError."""
    bad = SetupOpeningBalances(
        opening_date=date(2026, 4, 6),
        equity_account_code="3000",
        assets=[SetupOpeningBalanceItem(code="4000", balance_pence=1000)],
    )
    with pytest.raises(SetupError, match="not in the COA resolver"):
        await setup_entity(mock_db, ENTITY_ID, "uk_limited_company_vat", opening_balances=bad)
