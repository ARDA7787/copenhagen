"""Outbox delivery state: per-run ordering, claim leases, backoff and dead letters."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

JSON = sa.JSON().with_variant(JSONB, "postgresql")

revision = "0006"
down_revision = "0005"

# Frozen view of the table as of this revision, used only for the backfill.
outbox = sa.table(
    "dispatch_outbox",
    sa.column("tenant_id", sa.String()),
    sa.column("id", sa.String()),
    sa.column("data", JSON),
    sa.column("run_id", sa.String()),
)


def upgrade() -> None:
    with op.batch_alter_table("dispatch_outbox") as batch:
        batch.add_column(sa.Column("run_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_error", sa.String(), nullable=True))
        batch.create_index("ix_dispatch_outbox_run_id", ["run_id"])
        batch.create_index("ix_dispatch_outbox_next_attempt_at", ["next_attempt_at"])
    connection = op.get_bind()
    for tenant, id_, data in connection.execute(
        sa.select(outbox.c.tenant_id, outbox.c.id, outbox.c.data)
    ):
        connection.execute(
            outbox.update()
            .where(outbox.c.tenant_id == tenant, outbox.c.id == id_)
            .values(run_id=(data or {}).get("run_id"))
        )


def downgrade() -> None:
    raise RuntimeError("destructive downgrade refused")
