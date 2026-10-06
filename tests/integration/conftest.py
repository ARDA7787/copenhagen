import pytest


@pytest.fixture(autouse=True)
def _schema(migrated_test_database):
    """Every test here runs against PostgreSQL migrated to head."""
