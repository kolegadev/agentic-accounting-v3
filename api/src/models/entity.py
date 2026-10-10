"""SQLAlchemy model for Entity — accounting entity / company setup (Contract F11)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.config.database import Base

VALID_LEGAL_TYPES = (
    "sole_trader",
    "limited_company",
    "partnership",
    "micro_entity",
    "property_landlord",
    "other",
)
VALID_FRAMEWORKS = ("uk-gaap", "ifrs")
VALID_VAT_SCHEMES = ("standard", "cash", "flat_rate")
VALID_SETUP_STATES = ("none", "in_progress", "complete")


class Entity(Base):
    """An accounting entity (company) owning a ledger, COA and bank accounts.

    ``ledger_name`` defaults to ``entity-<uuid4hex[:8]>`` — derived from the
    entity's own id, assigned by :class:`src.services.entity_service.EntityService`
    at creation time.
    """

    __tablename__ = "entities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Entity (company) name",
    )
    legal_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="limited_company",
        server_default=text("'limited_company'"),
        comment="Legal form: sole_trader|limited_company|partnership|micro_entity|property_landlord|other",
    )
    framework: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="uk-gaap",
        server_default=text("'uk-gaap'"),
        comment="Reporting framework: uk-gaap|ifrs",
    )
    fiscal_year_start: Mapped[str] = mapped_column(
        String(5),
        nullable=False,
        default="04-06",
        server_default=text("'04-06'"),
        comment="Fiscal year start as MM-DD",
    )
    vat_registered: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        comment="Whether the entity is VAT registered",
    )
    vat_number: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
        comment="VAT registration number",
    )
    vat_scheme: Mapped[Optional[str]] = mapped_column(
        String(10),
        nullable=True,
        comment="VAT scheme: standard|cash|flat_rate",
    )
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default="GBP",
        server_default=text("'GBP'"),
        comment="ISO 4217 currency code",
    )
    ledger_name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Formance ledger name (default entity-<uuid4hex[:8]>)",
    )
    setup_state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="none",
        server_default=text("'none'"),
        comment="Setup lifecycle state: none|in_progress|complete",
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        comment="FK to users.id — the admin owner",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
        comment="Soft-delete flag",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=datetime.now(timezone.utc),
    )

    __table_args__ = (
        CheckConstraint(
            f"legal_type = ANY(ARRAY[{', '.join(repr(v) for v in VALID_LEGAL_TYPES)}])",
            name="ck_entities_legal_type",
        ),
        CheckConstraint(
            f"framework = ANY(ARRAY[{', '.join(repr(v) for v in VALID_FRAMEWORKS)}])",
            name="ck_entities_framework",
        ),
        CheckConstraint(
            f"vat_scheme IS NULL OR vat_scheme = ANY(ARRAY[{', '.join(repr(v) for v in VALID_VAT_SCHEMES)}])",
            name="ck_entities_vat_scheme",
        ),
        CheckConstraint(
            f"setup_state = ANY(ARRAY[{', '.join(repr(v) for v in VALID_SETUP_STATES)}])",
            name="ck_entities_setup_state",
        ),
        Index("ix_entities_name", "name", unique=True),
        Index("ix_entities_ledger_name", "ledger_name", unique=True),
        Index("ix_entities_created_by", "created_by"),
        Index("ix_entities_is_active", "is_active"),
    )

    def __repr__(self) -> str:
        return f"<Entity(id={self.id}, name={self.name!r}, ledger={self.ledger_name!r})>"
