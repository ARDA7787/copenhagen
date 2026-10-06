"""Control-plane state for durable runs and human decisions."""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

JSON = sa.JSON().with_variant(JSONB, "postgresql")
metadata = sa.MetaData()

revision = "0002"
down_revision = "0001"


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


TABLES: list[sa.Table] = []
TABLES.append(record("principals"))
TABLES.append(record("roles"))
TABLES.append(record("principal_roles"))
TABLES.append(record("credential_refs"))
TABLES.append(record("intents"))
TABLES.append(record("plans"))
TABLES.append(record("runs"))
TABLES.append(record("step_runs"))
TABLES.append(record("approvals"))
TABLES.append(record("human_tasks"))


def upgrade() -> None:
    connection = op.get_bind()
    for table in TABLES:
        table.create(connection)
        if connection.dialect.name == "postgresql":
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table.name} TO copenhagen_app")


def downgrade() -> None:
    raise RuntimeError("destructive downgrade refused")
