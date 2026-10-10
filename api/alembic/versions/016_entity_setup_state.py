"""016_entity_setup_state — add setup lifecycle state (Contract F12).

Revision ID: 016
Revises: 015
Create Date: 2026-10-10

Adds the ``setup_state`` lifecycle column to ``entities`` so initial-ledger
setup can be resumed after a partial failure instead of being locked out
permanently: none → in_progress → complete.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add setup_state with a none|in_progress|complete check."""
    op.add_column(
        "entities",
        sa.Column(
            "setup_state",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'none'"),
            comment="Setup lifecycle state: none|in_progress|complete",
        ),
    )
    op.create_check_constraint(
        "ck_entities_setup_state",
        "entities",
        sa.text("setup_state IN ('none', 'in_progress', 'complete')"),
    )


def downgrade() -> None:
    """Drop the setup_state column and its check constraint."""
    op.drop_constraint("ck_entities_setup_state", "entities", type_="check")
    op.drop_column("entities", "setup_state")
