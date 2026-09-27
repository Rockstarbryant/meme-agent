"""launchpad_tokens global registry + token_bookmarks + discovery checkpoints

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-27
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "launchpad_tokens",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("token_address", sa.String(length=128), nullable=False),
        sa.Column("launchpad", sa.String(length=64), nullable=True),
        sa.Column("launchpad_contract", sa.String(length=128), nullable=True),
        sa.Column("launch_event", sa.String(length=128), nullable=True),
        sa.Column("launch_tx_hash", sa.String(length=128), nullable=True),
        sa.Column("creator_address", sa.String(length=128), nullable=True),
        sa.Column("launched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="DISCOVERED"),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("initial_score", sa.Float(), nullable=True),
        sa.Column("current_score", sa.Float(), nullable=True),
        sa.Column("score_delta", sa.Float(), nullable=True),
        sa.Column("priority", sa.String(length=16), nullable=False, server_default="WARM"),
        sa.Column("last_monitored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_score_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("last_snapshot", sa.JSON(), nullable=True),
        sa.UniqueConstraint("chain", "token_address", name="uq_launchpad_tokens_chain_addr"),
    )
    op.create_index("ix_launchpad_tokens_status", "launchpad_tokens", ["status"])
    op.create_index("ix_launchpad_tokens_discovered_at", "launchpad_tokens", ["discovered_at"])
    op.create_index("ix_launchpad_tokens_launchpad", "launchpad_tokens", ["launchpad"])
    op.create_index("ix_launchpad_tokens_priority", "launchpad_tokens", ["priority"])

    op.create_table(
        "token_bookmarks",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("user_id", sa.String(length=32), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("chain", sa.String(length=32), nullable=False),
        sa.Column("token_address", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(length=512), nullable=True),
        sa.UniqueConstraint("user_id", "chain", "token_address", name="uq_token_bookmarks_user_token"),
    )
    op.create_index("ix_token_bookmarks_user", "token_bookmarks", ["user_id"])

    op.create_table(
        "discovery_checkpoints",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False, unique=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tokens_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # Premium scanner flag on users (nullable -> default false in app)
    op.add_column("users", sa.Column("premium_scanner", sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade() -> None:
    op.drop_column("users", "premium_scanner")
    op.drop_table("discovery_checkpoints")
    op.drop_table("token_bookmarks")
    op.drop_index("ix_launchpad_tokens_priority", table_name="launchpad_tokens")
    op.drop_index("ix_launchpad_tokens_launchpad", table_name="launchpad_tokens")
    op.drop_index("ix_launchpad_tokens_discovered_at", table_name="launchpad_tokens")
    op.drop_index("ix_launchpad_tokens_status", table_name="launchpad_tokens")
    op.drop_table("launchpad_tokens")
