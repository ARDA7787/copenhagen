"""Sessions, machine identity, pre-approvals, budgets and hook replay protection."""

from alembic import op

from copenhagen.db.models import ApiKey, AuditAnchor, Nonce, Preapproval, Session, Usage

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    connection = op.get_bind()
    for model in (Session, ApiKey, Preapproval, Usage, Nonce, AuditAnchor):
        model.__table__.create(connection)
        if connection.dialect.name == "postgresql":
            op.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON {model.__tablename__} TO copenhagen_app"
            )


def downgrade() -> None:
    raise RuntimeError("destructive downgrade refused")
