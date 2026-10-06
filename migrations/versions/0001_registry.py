"""Registry, tenant isolation, immutable specs and append-only audit grants."""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

JSON = sa.JSON().with_variant(JSONB, "postgresql")
metadata = sa.MetaData()

revision = "0001"
down_revision = None


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


def versioned(name: str) -> sa.Table:
    return record(
        name,
        sa.Column("name", sa.String(), nullable=False, index=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", "version"),
    )


tenants = sa.Table(
    "tenants",
    metadata,
    sa.Column("id", sa.String(), primary_key=True),
    sa.Column("name", sa.String(), nullable=False),
    sa.Column("config", JSON, nullable=False),
)
audit_events = sa.Table(
    "audit_events",
    metadata,
    sa.Column("tenant_id", sa.String(), primary_key=True),
    sa.Column("seq", sa.BigInteger(), primary_key=True, autoincrement=False),
    sa.Column("id", sa.String(), nullable=False),
    sa.Column("data", JSON, nullable=False),
    sa.Column("prev_hash", sa.String(), nullable=False),
    sa.Column("hash", sa.String(), nullable=False),
    sa.UniqueConstraint("tenant_id", "id"),
)
capabilities = versioned("capabilities")
recipes = versioned("recipes")


def upgrade() -> None:
    connection = op.get_bind()
    for table in (tenants, capabilities, recipes, audit_events):
        table.create(connection)
    if connection.dialect.name == "postgresql":
        op.execute("""CREATE FUNCTION cph_reject_audit_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'audit_events is append-only'; END $$""")
        op.execute("""CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE OR TRUNCATE
        ON audit_events
                      FOR EACH STATEMENT EXECUTE FUNCTION cph_reject_audit_mutation()""")
        op.execute("""CREATE FUNCTION cph_immutable_spec() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN IF NEW.data IS DISTINCT FROM OLD.data OR NEW.name <> OLD.name
        OR NEW.version <> OLD.version
        THEN RAISE EXCEPTION 'published versions are immutable'; END IF; RETURN NEW; END $$""")
        for table in ("capabilities", "recipes"):
            op.execute(
                f"CREATE TRIGGER immutable_spec BEFORE UPDATE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION cph_immutable_spec()"
            )
            op.execute(f"GRANT SELECT, INSERT, UPDATE(status) ON {table} TO copenhagen_app")
        op.execute("GRANT SELECT, INSERT ON audit_events TO copenhagen_app")
        op.execute("GRANT SELECT, INSERT, UPDATE ON tenants TO copenhagen_app")


def downgrade() -> None:
    raise RuntimeError("destructive downgrade refused; restore a reviewed backup")
