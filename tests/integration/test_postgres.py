"""Real app-role grants, immutable publication, chain serialization and tenant isolation."""

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from copenhagen.audit.chain import append, verify
from copenhagen.db.models import Tenant
from copenhagen.db.store import connect, new_id, transaction

pytestmark = pytest.mark.integration


@pytest.fixture
def postgres():
    db = connect(
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql+psycopg://copenhagen_app:copenhagen_app@localhost:5432/copenhagen_test",
        )
    )
    tenant = new_id("integration")
    with transaction(db) as session:
        session.add(Tenant(id=tenant, name="isolated integration test", config={}))
    yield db, tenant
    db.dispose()


@pytest.mark.invariant("I9")
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE audit_events SET hash=hash",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ],
)
def test_app_role_cannot_mutate_audit(postgres, sql):
    db, _ = postgres
    with (
        db.connect() as connection,
        pytest.raises(DBAPIError, match=r"permission denied|append-only"),
    ):
        connection.execute(text(sql))


def test_concurrent_writers_do_not_fork(postgres):
    db, tenant = postgres

    def write(index):
        with transaction(db) as session:
            append(session, tenant, f"event:{index}", "test.concurrent", value=index)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(40)))
    with transaction(db) as session:
        assert verify(session, tenant)["events"] == 40


def test_concurrent_budget_reservations_cannot_overspend(postgres):
    from copenhagen.policy.budgets import reserve, settle

    db, tenant = postgres

    def reserve_cost(index):
        with transaction(db) as session:
            return reserve(
                session,
                tenant,
                "principal:operator",
                f"request:{index}",
                {"money_cents": 60},
                {"money_cents": 100},
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        decisions = list(pool.map(reserve_cost, range(8)))
    assert sum(decisions) == 1
    winner = decisions.index(True)
    with transaction(db) as session:
        settle(session, tenant, "principal:operator", f"request:{winner}", commit=False)
    with transaction(db) as session:
        assert reserve(
            session,
            tenant,
            "principal:operator",
            "replacement",
            {"money_cents": 100},
            {"money_cents": 100},
        )
