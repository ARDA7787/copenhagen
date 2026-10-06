"""Authenticated, content-bound review of high-risk capability publication."""

from alembic import op

from copenhagen.db.models import PublicationReview

revision = "0005"
down_revision = "0004"


def upgrade() -> None:
    connection = op.get_bind()
    PublicationReview.__table__.create(connection)
    if connection.dialect.name == "postgresql":
        op.execute("GRANT SELECT, INSERT, UPDATE ON publication_reviews TO copenhagen_app")


def downgrade() -> None:
    raise RuntimeError("destructive downgrade refused")
