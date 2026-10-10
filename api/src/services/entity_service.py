"""Business logic for Entity CRUD — Contract F11."""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.entity import Entity
from src.validators.entity import EntityCreate, EntityUpdate

logger = logging.getLogger(__name__)


class EntityServiceError(Exception):
    """Base exception for entity service errors."""

    def __init__(
        self,
        message: str,
        status_code: int = 400,
        error_type: Optional[str] = None,
    ) -> None:
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        super().__init__(message)


class EntityNotFoundError(EntityServiceError):
    """Entity not found."""

    def __init__(self, entity_id: uuid.UUID) -> None:
        super().__init__(f"Entity '{entity_id}' not found", status_code=404)


class DuplicateEntityNameError(EntityServiceError):
    """An entity with this name already exists."""

    def __init__(self, name: str) -> None:
        super().__init__(f"An entity named '{name}' already exists", status_code=409)


class DuplicateLedgerNameError(EntityServiceError):
    """An entity with this ledger_name already exists."""

    def __init__(self, ledger_name: str) -> None:
        super().__init__(
            f"A ledger named '{ledger_name}' already exists",
            status_code=409,
            error_type="duplicate_ledger_name",
        )


class EntityService:
    """Stateless service for Entity CRUD."""

    @staticmethod
    async def _name_taken(
        db: AsyncSession,
        name: str,
        exclude_id: Optional[uuid.UUID] = None,
    ) -> bool:
        stmt = select(Entity).where(Entity.name == name)
        if exclude_id is not None:
            stmt = stmt.where(Entity.id != exclude_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def create_entity(
        db: AsyncSession,
        data: EntityCreate,
        *,
        created_by: uuid.UUID,
    ) -> Entity:
        """Create a new entity with the given user as admin owner.

        Derives ``ledger_name`` as ``entity-<uuid4hex[:8]>`` from the
        entity's own id unless an explicit ledger name is supplied.

        A colliding AUTO-DERIVED ledger name triggers a regenerate-and-retry
        (up to 3 retries); an explicit ledger name that collides raises
        :class:`DuplicateLedgerNameError` (409) immediately.
        """
        name = (data.name or "").strip()
        if not name:
            raise EntityServiceError("Entity name is required", status_code=400)

        if await EntityService._name_taken(db, name):
            raise DuplicateEntityNameError(name)

        explicit_ledger = bool(data.ledger_name)
        max_attempts = 4  # initial + up to 3 retries
        entity: Entity | None = None
        for attempt in range(max_attempts):
            entity_id = uuid.uuid4()
            ledger_name = data.ledger_name or f"entity-{entity_id.hex[:8]}"
            entity = Entity(
                id=entity_id,
                name=name,
                legal_type=data.legal_type,
                framework=data.framework,
                fiscal_year_start=data.fiscal_year_start,
                vat_registered=data.vat_registered,
                vat_number=data.vat_number,
                vat_scheme=data.vat_scheme,
                currency=data.currency,
                ledger_name=ledger_name,
                created_by=created_by,
            )
            db.add(entity)
            try:
                await db.commit()
                break
            except IntegrityError:
                await db.rollback()
                if explicit_ledger or attempt == max_attempts - 1:
                    raise DuplicateLedgerNameError(ledger_name) from None
                logger.warning(
                    "Auto-derived ledger name '%s' collided — regenerating (attempt %d)",
                    ledger_name,
                    attempt + 1,
                )

        await db.refresh(entity)
        return entity

    @staticmethod
    async def get_entity(
        db: AsyncSession,
        entity_id: uuid.UUID,
    ) -> Optional[Entity]:
        """Return a single entity by ID, or None if not found."""
        result = await db.execute(select(Entity).where(Entity.id == entity_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def list_entities(db: AsyncSession) -> list[Entity]:
        """List all entities, newest first."""
        result = await db.execute(
            select(Entity).order_by(Entity.created_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def update_entity(
        db: AsyncSession,
        entity_id: uuid.UUID,
        data: EntityUpdate,
    ) -> Entity:
        """Partially update an entity. Raises EntityNotFoundError."""
        entity = await EntityService.get_entity(db, entity_id)
        if entity is None:
            raise EntityNotFoundError(entity_id)

        update_data = data.model_dump(exclude_unset=True)

        if "name" in update_data:
            new_name = (update_data["name"] or "").strip()
            if not new_name:
                raise EntityServiceError("Entity name is required", status_code=400)
            if await EntityService._name_taken(db, new_name, exclude_id=entity_id):
                raise DuplicateEntityNameError(new_name)
            update_data["name"] = new_name

        for field, value in update_data.items():
            setattr(entity, field, value)

        await db.commit()
        await db.refresh(entity)
        return entity


async def get_active_ledger(db: AsyncSession) -> str:
    """Return the single active entity's ledger name, else ``"main"``.

    Deterministic resolution used for posting, parity and reports:
    exactly one active entity (``is_active=True``) → its ``ledger_name``;
    zero or multiple active entities → ``"main"``.  Any lookup failure
    (e.g. a pre-F11 schema without the ``entities`` table) degrades to
    ``"main"`` so the no-entity system keeps behaving exactly as before.
    """
    try:
        result = await db.execute(
            select(Entity).where(Entity.is_active.is_(True))
        )
        entities = list(result.scalars().all())
    except Exception as exc:  # noqa: BLE001 — resolution is best-effort
        logger.warning(
            "get_active_ledger: active-entity lookup failed (%s); "
            "falling back to 'main'",
            exc,
        )
        return "main"
    if len(entities) != 1:
        return "main"
    return entities[0].ledger_name
