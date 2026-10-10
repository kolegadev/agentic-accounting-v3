"""017_entity_scoping — add entity_id scoping columns (Contract F18).

Revision ID: 017
Revises: 016
Create Date: 2026-10-10

Adds ``entity_id UUID NULL REFERENCES entities(id)`` to ``accounts``,
``bank_accounts`` and ``transactions`` (+ a lookup index on each).
NULL = the legacy/global scope — no data migration needed: existing rows
stay NULL and keep behaving exactly as before.

Also relaxes the GLOBAL uniqueness of ``accounts.code`` (two entities must
be able to load the same template, i.e. share codes): the global unique
constraint/index are replaced by partial unique indexes —
``(entity_id, code)`` unique WHERE entity_id IS NOT NULL, and ``code``
unique WHERE entity_id IS NULL (legacy scope keeps its guarantee).
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("accounts", "bank_accounts", "transactions")


def upgrade() -> None:
    """Add entity_id + index + FK to the three scoped tables."""
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "entity_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
                comment="Owning accounting entity; NULL = legacy/global scope",
            ),
        )
        op.create_index(f"ix_{table}_entity_id", table, ["entity_id"])
        op.create_foreign_key(
            f"fk_{table}_entity_id_entities",
            table,
            "entities",
            ["entity_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # accounts.code: global uniqueness -> per-scope uniqueness (F18).
    op.drop_constraint("accounts_code_key", "accounts", type_="unique")
    op.drop_index("ix_accounts_code_unique", table_name="accounts")
    op.create_index("ix_accounts_code", "accounts", ["code"])
    op.create_index(
        "uq_accounts_entity_code",
        "accounts",
        ["entity_id", "code"],
        unique=True,
        postgresql_where=sa.text("entity_id IS NOT NULL"),
    )
    op.create_index(
        "uq_accounts_legacy_code",
        "accounts",
        ["code"],
        unique=True,
        postgresql_where=sa.text("entity_id IS NULL"),
    )


def downgrade() -> None:
    """Drop the FK, index and entity_id column from each table."""
    op.drop_index("uq_accounts_legacy_code", table_name="accounts")
    op.drop_index("uq_accounts_entity_code", table_name="accounts")
    op.drop_index("ix_accounts_code", table_name="accounts")
    op.create_index("ix_accounts_code_unique", "accounts", ["code"], unique=True)
    op.create_unique_constraint("accounts_code_key", "accounts", ["code"])

    for table in _TABLES:
        op.drop_constraint(
            f"fk_{table}_entity_id_entities", table, type_="foreignkey"
        )
        op.drop_index(f"ix_{table}_entity_id", table_name=table)
        op.drop_column(table, "entity_id")
