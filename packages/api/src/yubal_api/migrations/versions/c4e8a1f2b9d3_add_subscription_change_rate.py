"""Add subscription change_rate

Revision ID: c4e8a1f2b9d3
Revises: 8f2c1d7a4e60
Create Date: 2026-11-26 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e8a1f2b9d3"
down_revision: str | Sequence[str] | None = "8f2c1d7a4e60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "subscriptions",
        sa.Column("change_rate", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("subscriptions", "change_rate")
