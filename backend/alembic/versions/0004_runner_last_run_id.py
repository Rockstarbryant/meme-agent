"""runners.last_run_id (fixes event blackout after runner/worker restarts)

The runner's event `seq` is a local, per-process/per-disk AUTOINCREMENT
counter (see runner/store.py outbox table). It is only monotonic within one
continuously-running local outbox. If that local storage is recreated
(ephemeral disk, redeploy, crash/restart of a cloud worker container), the
counter restarts at 1 while the server-side `last_seq` watermark for that
Runner row stays at whatever high number the previous run reached. Every
subsequent event then looks like an old replay (`seq <= last_seq`) and is
silently skipped forever, even though `_handle()`'s own per-event id-based
idempotency check would have accepted it fine.

`last_run_id` lets the server detect "this is a new local sequence space"
(the runner now sends an opaque per-store run identifier with every batch)
and reset the seq baseline for that run instead of comparing across runs.
See app/services/ingest.py and runner/runtime.py.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-26
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runners", sa.Column("last_run_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("runners", "last_run_id")
