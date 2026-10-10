"""ops_audit_log table + users.is_admin

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        "ops_audit_log",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=False),
        sa.Column("component", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("provider", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("model", sa.String(length=96), nullable=False, server_default=""),
        sa.Column("operation", sa.String(length=48), nullable=False, server_default=""),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("token_key", sa.String(length=96), nullable=False, server_default=""),
        sa.Column("user_id", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("decision_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("repeat", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("detail", sa.JSON(), nullable=False, server_default="{}"),
    )
    for col in ("at", "kind", "status", "severity", "provider", "token_key", "user_id", "decision_id"):
        op.create_index(f"ix_ops_audit_log_{col}", "ops_audit_log", [col])


def downgrade() -> None:
    op.drop_table("ops_audit_log")
    op.drop_column("users", "is_admin")
