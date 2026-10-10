"""015_entities — Create entities table for Entity Setup (Contract F11).

Revision ID: 015
Revises: 014
Create Date: 2026-10-10

Creates the entities table holding accounting entities (companies) with
framework/fiscal/VAT settings and the admin owner (created_by FK to users).
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "015"
down_revision: Union[str, None] = "014_contacts_other_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

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


def upgrade() -> None:
    """Create entities table."""
    op.create_table(
        "entities",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "name",
            sa.String(255),
            nullable=False,
            comment="Entity (company) name",
        ),
        sa.Column(
            "legal_type",
            sa.String(30),
            nullable=False,
            server_default=sa.text("'limited_company'"),
            comment="Legal form: sole_trader|limited_company|partnership|micro_entity|property_landlord|other",
        ),
        sa.Column(
            "framework",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'uk-gaap'"),
            comment="Reporting framework: uk-gaap|ifrs",
        ),
        sa.Column(
            "fiscal_year_start",
            sa.String(5),
            nullable=False,
            server_default=sa.text("'04-06'"),
            comment="Fiscal year start as MM-DD",
        ),
        sa.Column(
            "vat_registered",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
            comment="Whether the entity is VAT registered",
        ),
        sa.Column(
            "vat_number",
            sa.String(20),
            nullable=True,
            comment="VAT registration number",
        ),
        sa.Column(
            "vat_scheme",
            sa.String(10),
            nullable=True,
            comment="VAT scheme: standard|cash|flat_rate",
        ),
        sa.Column(
            "currency",
            sa.String(3),
            nullable=False,
            server_default=sa.text("'GBP'"),
            comment="ISO 4217 currency code",
        ),
        sa.Column(
            "ledger_name",
            sa.String(64),
            nullable=False,
            comment="Formance ledger name (default entity-<uuid4hex[:8]>)",
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
            comment="FK to users.id — the admin owner",
        ),
        sa.Column(
            "is_active",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("true"),
            comment="Soft-delete flag",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_check_constraint(
        "ck_entities_legal_type",
        "entities",
        sa.text(
            f"legal_type = ANY(ARRAY[{', '.join(repr(v) for v in VALID_LEGAL_TYPES)}])"
        ),
    )
    op.create_check_constraint(
        "ck_entities_framework",
        "entities",
        sa.text(
            f"framework = ANY(ARRAY[{', '.join(repr(v) for v in VALID_FRAMEWORKS)}])"
        ),
    )
    op.create_check_constraint(
        "ck_entities_vat_scheme",
        "entities",
        sa.text(
            f"vat_scheme IS NULL OR vat_scheme = ANY(ARRAY[{', '.join(repr(v) for v in VALID_VAT_SCHEMES)}])"
        ),
    )

    op.create_index("ix_entities_name", "entities", ["name"], unique=True)
    op.create_index("ix_entities_ledger_name", "entities", ["ledger_name"], unique=True)
    op.create_index("ix_entities_created_by", "entities", ["created_by"])
    op.create_index("ix_entities_is_active", "entities", ["is_active"])


def downgrade() -> None:
    """Drop entities table."""
    op.drop_table("entities")
