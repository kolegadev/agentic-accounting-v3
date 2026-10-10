"""Unit tests for EntityService with mocked DB session (Contract F11)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models.entity import Entity
from src.services.entity_service import (
    DuplicateEntityNameError,
    EntityService,
    EntityServiceError,
    EntityNotFoundError,
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
