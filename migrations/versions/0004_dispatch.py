"""Durable delivery of run starts and human decisions to Temporal."""

from alembic import op

from copenhagen.db.models import Outbox

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    connection = op.get_bind()
    Outbox.__table__.create(connection)
    if connection.dialect.name == "postgresql":
        op.execute("GRANT SELECT, INSERT, UPDATE ON dispatch_outbox TO copenhagen_app")


def downgrade() -> None:
    raise RuntimeError("destructive downgrade refused")
