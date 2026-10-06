"""Control-plane state for durable runs and human decisions."""

from alembic import op

from copenhagen.db.models import (
    Approval,
    CredentialRef,
    HumanTask,
    Intent,
    Plan,
    Principal,
    PrincipalRole,
    Role,
    Run,
    StepRun,
)

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    connection = op.get_bind()
    for model in (
        Principal,
        Role,
        PrincipalRole,
        CredentialRef,
        Intent,
        Plan,
        Run,
        StepRun,
        Approval,
        HumanTask,
    ):
        model.__table__.create(connection)
        if connection.dialect.name == "postgresql":
            op.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON {model.__tablename__} TO copenhagen_app"
            )


def downgrade() -> None:
    raise RuntimeError("destructive downgrade refused")
