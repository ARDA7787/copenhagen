"""Local model/service tests use isolated SQLite files; PostgreSQL tests are separate."""

import pytest

from copenhagen.db.models import Base
from copenhagen.db.store import connect
from copenhagen.service import Service
from copenhagen.settings import Settings
from copenhagen_devkit.seed import seed


@pytest.fixture
def service(tmp_path):
    db = connect(f"sqlite:///{tmp_path}/control.sqlite")
    Base.metadata.create_all(db)
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        env="test",
        database_url=str(db.url),
        copenhagen_dev_login=True,
        hook_secret="test-only-hook-key",
    )
    result = Service(db, settings)
    seed(result)
    yield result
    db.dispose()


@pytest.fixture
def company(tmp_path):
    from copenhagen.administration import (
        PrincipalInput,
        RoleInput,
        define_role,
        initialize,
        provision,
    )

    db = connect(f"sqlite:///{tmp_path}/company.sqlite")
    Base.metadata.create_all(db)
    svc = Service(db, Settings(env="test", tenant_id="business", copenhagen_dev_login=True))
    initialize(svc, "Business", PrincipalInput(id="operator", email="op@business.example"))
    provision(svc, "operator", PrincipalInput(id="reviewer", email="review@business.example"))
    define_role(svc, "operator", RoleInput(name="approver:ops"))
    svc.assign_role("operator", "reviewer", "approver:ops", True)
    yield svc
    db.dispose()
