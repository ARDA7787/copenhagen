from alembic import context
from sqlalchemy import create_engine

from copenhagen.db.models import Base
from copenhagen.settings import Settings

settings = Settings()
url = settings.database_owner_url or settings.db_url
engine = create_engine(url.replace("postgresql+asyncpg:", "postgresql+psycopg:"))
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
