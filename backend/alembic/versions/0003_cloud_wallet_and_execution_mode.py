"""cloud wallet external_id + agent execution_mode

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("wallets", sa.Column("external_id", sa.String(length=128), nullable=True))
    op.create_index("ix_wallets_external_id", "wallets", ["external_id"])
    op.add_column(
        "agent_configs",
        sa.Column("execution_mode", sa.String(length=16), nullable=False, server_default="self_hosted"),
    )


def downgrade() -> None:
    op.drop_column("agent_configs", "execution_mode")
    op.drop_index("ix_wallets_external_id", table_name="wallets")
    op.drop_column("wallets", "external_id")
