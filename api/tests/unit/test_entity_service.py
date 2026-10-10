"""Unit tests for EntityService with mocked DB session (Contract F11/F12)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from src.models.entity import Entity
from src.services.entity_service import (
    DuplicateEntityNameError,
    DuplicateLedgerNameError,
    EntityService,
    EntityServiceError,
    EntityNotFoundError,
    get_active_ledger,
)
from src.validators.entity import EntityCreate, EntityUpdate
from tests.conftest import NOW


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db() -> AsyncMock:
    """AsyncMock DB session whose refresh populates server-default fields."""
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()

    async def _refresh(obj: object) -> None:
        if hasattr(obj, "id") and obj.id is None:
            obj.id = uuid.uuid4()  # type: ignore[attr-defined]
        if hasattr(obj, "is_active") and obj.is_active is None:
            obj.is_active = True  # type: ignore[attr-defined]
        if hasattr(obj, "created_at") and obj.created_at is None:
            obj.created_at = NOW  # type: ignore[attr-defined]
        if hasattr(obj, "updated_at") and obj.updated_at is None:
            obj.updated_at = NOW  # type: ignore[attr-defined]

    db.refresh = _refresh
    return db


@pytest.fixture
def owner_id() -> uuid.UUID:
    return uuid.uuid4()


def make_entity(**overrides) -> Entity:
    defaults = {
        "id": uuid.uuid4(),
        "name": "Acme Holdings Ltd",
        "legal_type": "limited_company",
        "framework": "uk-gaap",
        "fiscal_year_start": "04-06",
        "vat_registered": False,
        "vat_number": None,
        "vat_scheme": None,
        "currency": "GBP",
        "ledger_name": "entity-deadbeef",
        "created_by": uuid.uuid4(),
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    defaults.update(overrides)
    return Entity(**defaults)


def make_create(**overrides) -> EntityCreate:
    defaults = {
        "name": "Acme Holdings Ltd",
        "legal_type": "limited_company",
        "framework": "uk-gaap",
        "fiscal_year_start": "04-06",
        "vat_registered": False,
        "vat_scheme": None,
        "currency": "GBP",
    }
    defaults.update(overrides)
    return EntityCreate(**defaults)


def _no_existing_entity(db: AsyncMock) -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result


# ---------------------------------------------------------------------------
# create_entity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_entity_requires_name(mock_db: AsyncMock, owner_id) -> None:
    """Empty/whitespace name must raise EntityServiceError."""
    with pytest.raises(EntityServiceError, match="name is required"):
        await EntityService.create_entity(
            mock_db, make_create(name="   "), created_by=owner_id
        )


@pytest.mark.asyncio
async def test_create_entity_duplicate_name_409(mock_db: AsyncMock, owner_id) -> None:
    """Duplicate name must raise 409."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = make_entity()
    mock_db.execute.return_value = result

    with pytest.raises(DuplicateEntityNameError) as excinfo:
        await EntityService.create_entity(
            mock_db, make_create(), created_by=owner_id
        )
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_create_entity_derives_ledger_name(mock_db: AsyncMock, owner_id) -> None:
    """ledger_name defaults to entity-<uuid4hex[:8]> of the entity's own id."""
    _no_existing_entity(mock_db)
    entity = await EntityService.create_entity(
        mock_db, make_create(), created_by=owner_id
    )

    assert entity.id is not None
    assert entity.ledger_name == f"entity-{entity.id.hex[:8]}"
    assert entity.created_by == owner_id
    assert entity.name == "Acme Holdings Ltd"
    assert entity.legal_type == "limited_company"
    assert entity.framework == "uk-gaap"
    assert entity.fiscal_year_start == "04-06"
    assert entity.currency == "GBP"
    assert entity.is_active is True


@pytest.mark.asyncio
async def test_create_entity_explicit_ledger_name(mock_db: AsyncMock, owner_id) -> None:
    """An explicit ledger_name must be kept verbatim."""
    _no_existing_entity(mock_db)
    entity = await EntityService.create_entity(
        mock_db, make_create(ledger_name="acme-main"), created_by=owner_id
    )
    assert entity.ledger_name == "acme-main"


@pytest.mark.asyncio
async def test_create_entity_trims_name(mock_db: AsyncMock, owner_id) -> None:
    """The name is stripped before being stored."""
    _no_existing_entity(mock_db)
    entity = await EntityService.create_entity(
        mock_db, make_create(name="  Acme Holdings Ltd  "), created_by=owner_id
    )
    assert entity.name == "Acme Holdings Ltd"


@pytest.mark.asyncio
async def test_create_entity_auto_ledger_collision_retries(
    mock_db: AsyncMock, owner_id
) -> None:
    """An AUTO-DERIVED ledger_name collision regenerates and retries."""
    _no_existing_entity(mock_db)
    mock_db.commit = AsyncMock(
        side_effect=[IntegrityError("INSERT", {}, Exception("duplicate")), None]
    )
    mock_db.rollback = AsyncMock()

    added: list[Entity] = []
    mock_db.add = added.append

    entity = await EntityService.create_entity(
        mock_db, make_create(), created_by=owner_id
    )

    assert entity.ledger_name == f"entity-{entity.id.hex[:8]}"
    assert mock_db.commit.await_count == 2
    mock_db.rollback.assert_awaited_once()
    assert len(added) == 2
    assert added[0].ledger_name != added[1].ledger_name
    assert entity.id == added[1].id


@pytest.mark.asyncio
async def test_create_entity_auto_ledger_collision_exhausts_retries(
    mock_db: AsyncMock, owner_id
) -> None:
    """After the retry budget is exhausted, a 409 surfaces."""
    _no_existing_entity(mock_db)
    mock_db.commit = AsyncMock(
        side_effect=IntegrityError("INSERT", {}, Exception("duplicate"))
    )
    mock_db.rollback = AsyncMock()

    with pytest.raises(DuplicateLedgerNameError) as excinfo:
        await EntityService.create_entity(mock_db, make_create(), created_by=owner_id)

    assert excinfo.value.status_code == 409
    assert excinfo.value.error_type == "duplicate_ledger_name"
    assert mock_db.commit.await_count == 4  # initial + 3 retries


@pytest.mark.asyncio
async def test_create_entity_explicit_ledger_collision_no_retry(
    mock_db: AsyncMock, owner_id
) -> None:
    """An EXPLICIT ledger_name collision fails immediately — no retry."""
    _no_existing_entity(mock_db)
    mock_db.commit = AsyncMock(
        side_effect=IntegrityError("INSERT", {}, Exception("duplicate"))
    )
    mock_db.rollback = AsyncMock()

    with pytest.raises(DuplicateLedgerNameError) as excinfo:
        await EntityService.create_entity(
            mock_db, make_create(ledger_name="acme-main"), created_by=owner_id
        )

    assert excinfo.value.status_code == 409
    assert excinfo.value.error_type == "duplicate_ledger_name"
    assert mock_db.commit.await_count == 1


# ---------------------------------------------------------------------------
# get_active_ledger
# ---------------------------------------------------------------------------

def _entities_result(entities: list[Entity]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = entities
    return result


@pytest.mark.asyncio
async def test_get_active_ledger_no_entity_returns_main(mock_db: AsyncMock) -> None:
    mock_db.execute.return_value = _entities_result([])
    assert await get_active_ledger(mock_db) == "main"


@pytest.mark.asyncio
async def test_get_active_ledger_single_entity(mock_db: AsyncMock) -> None:
    entity = make_entity(ledger_name="entity-abc123")
    mock_db.execute.return_value = _entities_result([entity])
    assert await get_active_ledger(mock_db) == "entity-abc123"


@pytest.mark.asyncio
async def test_get_active_ledger_multiple_entities_returns_main(
    mock_db: AsyncMock,
) -> None:
    e1 = make_entity()
    e2 = make_entity(name="Beta LLP")
    mock_db.execute.return_value = _entities_result([e1, e2])
    assert await get_active_ledger(mock_db) == "main"


@pytest.mark.asyncio
async def test_get_active_ledger_ignores_inactive_entities(
    mock_db: AsyncMock,
) -> None:
    """An INACTIVE-only entity still resolves to 'main' (the mock models the
    WHERE is_active filter by returning the post-filter row set)."""
    mock_db.execute.return_value = _entities_result([])
    assert await get_active_ledger(mock_db) == "main"


@pytest.mark.asyncio
async def test_get_active_ledger_prefers_the_single_active_entity(
    mock_db: AsyncMock,
) -> None:
    """The one ACTIVE entity wins even when inactive entities exist."""
    active = make_entity(ledger_name="entity-abc123", is_active=True)
    mock_db.execute.return_value = _entities_result([active])
    assert await get_active_ledger(mock_db) == "entity-abc123"


@pytest.mark.asyncio
async def test_get_active_ledger_query_failure_returns_main(
    mock_db: AsyncMock,
) -> None:
    """A lookup failure (e.g. no entities table) degrades to 'main'."""
    mock_db.execute = AsyncMock(side_effect=RuntimeError("no such table"))
    assert await get_active_ledger(mock_db) == "main"


# ---------------------------------------------------------------------------
# get_entity / list_entities
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_entity_found(mock_db: AsyncMock) -> None:
    entity = make_entity()
    result = MagicMock()
    result.scalar_one_or_none.return_value = entity
    mock_db.execute.return_value = result

    assert await EntityService.get_entity(mock_db, entity.id) is entity


@pytest.mark.asyncio
async def test_get_entity_not_found(mock_db: AsyncMock) -> None:
    _no_existing_entity(mock_db)
    assert await EntityService.get_entity(mock_db, uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_list_entities(mock_db: AsyncMock) -> None:
    e1, e2 = make_entity(), make_entity(name="Beta LLP", legal_type="partnership")
    result = MagicMock()
    result.scalars.return_value.all.return_value = [e1, e2]
    mock_db.execute.return_value = result

    entities = await EntityService.list_entities(mock_db)
    assert entities == [e1, e2]


# ---------------------------------------------------------------------------
# update_entity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_entity_unknown_id_404(mock_db: AsyncMock) -> None:
    _no_existing_entity(mock_db)
    with pytest.raises(EntityNotFoundError) as excinfo:
        await EntityService.update_entity(
            mock_db, uuid.uuid4(), EntityUpdate(vat_registered=True)
        )
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_update_entity_success(mock_db: AsyncMock) -> None:
    entity = make_entity()
    result = MagicMock()
    result.scalar_one_or_none.return_value = entity
    mock_db.execute.return_value = result

    updated = await EntityService.update_entity(
        mock_db, entity.id, EntityUpdate(vat_registered=True, vat_scheme="cash")
    )
    assert updated.vat_registered is True
    assert updated.vat_scheme == "cash"
    assert updated.name == "Acme Holdings Ltd"  # untouched


@pytest.mark.asyncio
async def test_update_entity_duplicate_name_409(mock_db: AsyncMock) -> None:
    """Renaming to an existing entity's name must raise 409."""
    entity = make_entity()
    other = make_entity(name="Beta LLP")

    def _execute(stmt, *args, **kwargs):
        result = MagicMock()
        text = str(stmt)
        if "entities.name" in text and "entities.id !=" in text:
            result.scalar_one_or_none.return_value = other  # name taken by other
        else:
            result.scalar_one_or_none.return_value = entity
        return result

    mock_db.execute.side_effect = _execute

    with pytest.raises(DuplicateEntityNameError):
        await EntityService.update_entity(
            mock_db, entity.id, EntityUpdate(name="Beta LLP")
        )


@pytest.mark.asyncio
async def test_update_entity_empty_name_400(mock_db: AsyncMock) -> None:
    entity = make_entity()
    result = MagicMock()
    result.scalar_one_or_none.return_value = entity
    mock_db.execute.return_value = result

    with pytest.raises(EntityServiceError, match="name is required"):
        await EntityService.update_entity(mock_db, entity.id, EntityUpdate(name=" "))
