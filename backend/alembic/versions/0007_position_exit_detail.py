"""positions.exit_detail — AI / rule exit rationale shown on closed positions

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-12
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("exit_detail", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("positions", "exit_detail")
