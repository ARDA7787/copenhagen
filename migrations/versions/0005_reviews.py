"""Authenticated, content-bound review of high-risk capability publication."""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

JSON = sa.JSON().with_variant(JSONB, "postgresql")
metadata = sa.MetaData()

revision = "0005"
down_revision = "0004"


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


TABLE = record("publication_reviews")


def upgrade() -> None:
    connection = op.get_bind()
    TABLE.create(connection)
    if connection.dialect.name == "postgresql":
        op.execute("GRANT SELECT, INSERT, UPDATE ON publication_reviews TO copenhagen_app")


def downgrade() -> None:
    raise RuntimeError("destructive downgrade refused")
