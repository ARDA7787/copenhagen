"""Sessions, machine identity, pre-approvals, budgets and hook replay protection."""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

JSON = sa.JSON().with_variant(JSONB, "postgresql")
metadata = sa.MetaData()

revision = "0003"
down_revision = "0002"


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
TABLES.append(record("sessions"))
TABLES.append(record("api_keys"))
TABLES.append(record("recipe_preapprovals"))
TABLES.append(record("usage_counters"))
TABLES.append(record("hook_nonces"))
TABLES.append(record("audit_anchors"))


def upgrade() -> None:
    connection = op.get_bind()
    for table in TABLES:
        table.create(connection)
        if connection.dialect.name == "postgresql":
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table.name} TO copenhagen_app")


def downgrade() -> None:
    raise RuntimeError("destructive downgrade refused")
