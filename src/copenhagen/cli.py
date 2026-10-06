"""Operator CLI. Local database commands require explicit operator-level access."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import httpx
import typer

from copenhagen.core.capability import CapabilitySpec
from copenhagen.core.plan import PlanIR
from copenhagen.core.recipe import RecipeSpec
from copenhagen.registry.loader import load

app = typer.Typer(help="Copenhagen governed runbooks")
cap_app = typer.Typer()
audit_app = typer.Typer()
plan_app = typer.Typer()
run_app = typer.Typer()
dev_app = typer.Typer()
admin_app = typer.Typer(help="Local operator administration (requires database access)")
recipe_app = typer.Typer()
app.add_typer(admin_app, name="admin")
app.add_typer(recipe_app, name="recipe")
app.add_typer(cap_app, name="capability")
app.add_typer(audit_app, name="audit")
app.add_typer(plan_app, name="plan")
app.add_typer(run_app, name="run")
app.add_typer(dev_app, name="dev")


def service():
    from copenhagen.db.store import connect
    from copenhagen.service import Service
    from copenhagen.settings import Settings

    settings = Settings()
    return Service(connect(settings.db_url), settings)


def output(value) -> None:
    typer.echo(json.dumps(value, indent=2, default=str))


def pairs(values: list[str]) -> dict:
    result = {}
    for item in values:
        key, sep, raw = item.partition("=")
        if not sep:
            raise typer.BadParameter("use key=value")
        try:
            result[key] = json.loads(raw)
        except json.JSONDecodeError:
            result[key] = raw
    return result


@cap_app.command("validate")
def cap_validate(path: Path):
    spec = load(path, CapabilitySpec)
    output({"valid": True, "capability": spec.ref})


@cap_app.command("publish")
def cap_publish(path: Path, actor: str, second_reviewer: str | None = None):
    from copenhagen.db.store import transaction
    from copenhagen.registry.publish import publish

    svc = service()
    with transaction(svc.engine) as session:
        identity = svc.identity(session, actor)
        if not {"admin", "capability_author"} & set(identity.roles):
            raise typer.BadParameter("capability author or admin role required")
        if second_reviewer:
            reviewer = svc.identity(session, second_reviewer)
            if not {"admin", "capability_author"} & set(reviewer.roles):
                raise typer.BadParameter("second reviewer needs publishing authority")
        output(
            {
                "published": publish(
                    session, svc.tenant, load(path, CapabilitySpec), actor, second_reviewer
                )
            }
        )


@cap_app.command("status")
def cap_status(ref: str, status: str, actor: str):
    from copenhagen.db.store import transaction
    from copenhagen.registry.publish import set_status

    svc = service()
    with transaction(svc.engine) as session:
        if "admin" not in svc.identity(session, actor).roles:
            raise typer.BadParameter("admin role required")
        set_status(session, svc.tenant, ref, status, actor)
    output({"updated": True})


@audit_app.command("verify")
def audit_verify():
    from copenhagen.audit.chain import verify
    from copenhagen.db.store import transaction

    svc = service()
    with transaction(svc.engine) as session:
        output(verify(session, svc.tenant))


@audit_app.command("check")
def audit_check():
    from copenhagen.audit.chain import check_invariants
    from copenhagen.db.store import transaction

    svc = service()
    with transaction(svc.engine) as session:
        output(check_invariants(session, svc.tenant))


@audit_app.command("export")
def audit_export(path: Path):
    from copenhagen.audit.chain import events
    from copenhagen.db.store import transaction

    svc = service()
    with transaction(svc.engine) as session:
        data = [
            {**e.data, "hash": e.hash, "prev_hash": e.prev_hash}
            for e in events(session, svc.tenant)
        ]
    path.write_text("\n".join(json.dumps(e) for e in data) + "\n")
    output({"path": str(path.resolve()), "events": len(data)})


@dev_app.command("seed")
def dev_seed():
    from copenhagen.seed import seed

    seed(service())
    output({"seeded": True})


@dev_app.command("demo")
def dev_demo():
    from copenhagen.demo import demo

    asyncio.run(demo())


@app.command("serve")
def serve(host: str = "127.0.0.1", port: int = 8000):
    import uvicorn

    uvicorn.run("copenhagen.api.app:create_app", factory=True, host=host, port=port)


@app.command("control-worker")
def control_worker():
    from copenhagen.runtime import control_worker as run

    asyncio.run(run())


@app.command("worker")
def worker(queue: str):
    from copenhagen.runtime import domain_worker

    asyncio.run(domain_worker(queue))


@app.command("mockworld")
def mockworld(port: int = 8010):
    import uvicorn

    uvicorn.run("copenhagen.mockworld:create_app", factory=True, host="127.0.0.1", port=port)


@run_app.command("recipe")
def run_recipe(
    name: str,
    param: Annotated[list[str] | None, typer.Option()] = None,
    api_key: Annotated[str, typer.Option(envvar="COPENHAGEN_API_KEY")] = "",
    url: str = "http://localhost:8000",
):
    if param is None:
        param = []
    response = httpx.post(
        url + f"/v1/recipes/{name}/run",
        json={"parameters": pairs(param)},
        headers={"Authorization": "Bearer " + api_key},
        timeout=30,
    )
    response.raise_for_status()
    output(response.json())


@plan_app.command("validate")
def plan_validate(path: Path, actor: str | None = None, syntax_only: bool = False):
    """Check a plan against the registry and policy, or explicitly check syntax only."""
    plan = PlanIR.model_validate_json(path.read_text())
    if syntax_only:
        output({"valid": True, "steps": len(plan.steps), "scope": "syntax"})
        return
    if not actor:
        raise typer.BadParameter(
            "--actor is required for policy validation; use --syntax-only for syntax"
        )
    result = service().preview(actor, plan=plan)
    output(result)
    if result["preview"]["blocked"]:
        raise typer.Exit(1)


@plan_app.command("submit")
def plan_submit(
    path: Path,
    api_key: Annotated[str, typer.Option(envvar="COPENHAGEN_API_KEY")],
    url: str = "http://localhost:8000",
):
    response = httpx.post(
        url + "/v1/plans",
        json=json.loads(path.read_text()),
        headers={"Authorization": "Bearer " + api_key},
        timeout=30,
    )
    response.raise_for_status()
    output(response.json())


@app.command("runs")
def runs(
    id_: str = "",
    api_key: Annotated[str, typer.Option(envvar="COPENHAGEN_API_KEY")] = "",
    url: str = "http://localhost:8000",
):
    response = httpx.get(
        url + "/v1/runs" + ("/" + id_ if id_ else ""),
        headers={"Authorization": "Bearer " + api_key},
        timeout=30,
    )
    response.raise_for_status()
    output(response.json())


@app.command("schemas")
def schemas():
    from copenhagen.policy.engine import schema

    Path("schemas").mkdir(exist_ok=True)
    for name, model in [("capability", CapabilitySpec), ("recipe", RecipeSpec), ("plan", PlanIR)]:
        Path(f"schemas/{name}.schema.json").write_text(
            json.dumps(model.model_json_schema(by_alias=True), indent=2) + "\n"
        )
    Path("policies/schema.json").write_text(json.dumps(schema(), indent=2) + "\n")


@app.command("call")
def call(ref: str, actor: str, input_: Annotated[list[str] | None, typer.Option("--input")] = None):
    """Dev-only call through the exact same policy/audit/Temporal path as a plan."""
    if input_ is None:
        input_ = []
    svc = service()
    if svc.settings.env == "prod":
        raise typer.BadParameter("direct calls are development-only")
    from copenhagen.engine.client import TemporalEngine

    async def run():
        engine = await TemporalEngine.connect(svc.settings)
        preview = svc.preview(
            actor,
            plan=PlanIR.model_validate(
                {"steps": [{"id": "call", "capability": ref, "inputs": pairs(input_)}]}
            ),
        )
        envelope = svc.confirm(actor, preview["id"])
        await engine.start(envelope)
        output({"run_id": envelope.run_id, "preview": preview["preview"]})

    asyncio.run(run())


@app.command("init")
def initialize(name: str, admin_id: str, admin_email: str, idp_subject: str | None = None):
    """Initialize an empty company workspace. No demo data is installed."""
    from copenhagen.administration import PrincipalInput
    from copenhagen.administration import initialize as bootstrap

    bootstrap(
        service(), name, PrincipalInput(id=admin_id, email=admin_email, idp_subject=idp_subject)
    )
    output({"initialized": True, "administrator": admin_id})


@admin_app.command("principal")
def provision_principal(path: Path, actor: str):
    from copenhagen.administration import PrincipalInput, provision

    provision(service(), actor, load(path, PrincipalInput))
    output({"created": True})


@admin_app.command("role")
def define_role(path: Path, actor: str):
    from copenhagen.administration import RoleInput
    from copenhagen.administration import define_role as define

    define(service(), actor, load(path, RoleInput))
    output({"updated": True})


@admin_app.command("grant")
def grant_role(principal: str, role: str, actor: str, enabled: bool = True):
    service().assign_role(actor, principal, role, enabled)
    output({"updated": True})


@admin_app.command("configure")
def configure_company(path: Path, actor: str):
    from copenhagen.administration import TenantConfig, configure

    configure(service(), actor, load(path, TenantConfig))
    output({"updated": True})


@admin_app.command("key")
def issue_key(principal: str, scope: list[str], hours: int = 24):
    """Issue a machine credential; displayed only once. Protect terminal output."""
    output({"api_key": service().issue_key(principal, scope, hours)})


@recipe_app.command("validate")
def recipe_validate(path: Path):
    output({"valid": True, "recipe": load(path, RecipeSpec).ref})


@recipe_app.command("publish")
def recipe_publish(path: Path, actor: str):
    from copenhagen.db.store import transaction
    from copenhagen.registry.publish import publish

    svc = service()
    with transaction(svc.engine) as session:
        identity = svc.identity(session, actor)
        if not {"admin", "capability_author"} & set(identity.roles):
            raise typer.BadParameter("publishing authority required")
        output({"published": publish(session, svc.tenant, load(path, RecipeSpec), actor)})


if __name__ == "__main__":
    app()
