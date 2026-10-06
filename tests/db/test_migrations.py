"""Migrations are frozen history; a fresh upgrade must produce exactly the ORM schema."""

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from copenhagen.db.models import Base

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def upgraded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite:///{tmp_path / 'migrated.sqlite'}"
    # Belt and braces: never let a developer .env point this test at a real database.
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATABASE_OWNER_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")
    return url


def test_fresh_upgrade_matches_models(upgraded: str):
    engine = create_engine(upgraded)
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == []


def test_migrations_do_not_import_live_models():
    for path in (ROOT / "migrations" / "versions").glob("*.py"):
        assert "copenhagen.db.models" not in path.read_text(), path.name
