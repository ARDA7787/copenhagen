"""Durable delivery of run starts and human decisions to Temporal."""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

JSON = sa.JSON().with_variant(JSONB, "postgresql")
metadata = sa.MetaData()

revision = "0004"
down_revision = "0003"


def record(name: str, *extra: Any) -> sa.Table:
    """Frozen copy of the Record shape as of this revision. Never edit after release."""
    return sa.Table(
        name,
        metadata,
        sa.Column("tenant_id", sa.String(), primary_key=True),
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("data", JSON, nullable=False),
        sa.Column("status", sa.String(), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        *extra,
    )


TABLE = record("dispatch_outbox")


def upgrade() -> None:
    connection = op.get_bind()
    TABLE.create(connection)
    if connection.dialect.name == "postgresql":
        op.execute("GRANT SELECT, INSERT, UPDATE ON dispatch_outbox TO copenhagen_app")


def downgrade() -> None:
    raise RuntimeError("destructive downgrade refused")
