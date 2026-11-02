"""Add jobs table

Revision ID: 8f2c1d7a4e60
Revises: 03132d5514f9
Create Date: 2026-11-02 19:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f2c1d7a4e60"
down_revision: str | Sequence[str] | None = "03132d5514f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "jobs",
        sa.Column("id", sa.VARCHAR(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("data", sa.VARCHAR(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_jobs_position"), "jobs", ["position"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_jobs_position"), table_name="jobs")
    op.drop_table("jobs")
