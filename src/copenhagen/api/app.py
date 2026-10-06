"""Milestone A API and server-rendered runbook application."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import Engine

from copenhagen.administration import (
    PrincipalInput,
    RoleInput,
    TenantConfig,
    configure,
    define_role,
    disable_principal,
    provision,
)
from copenhagen.api.auth import COOKIE, SessionMiddleware, actor, login
from copenhagen.audit.chain import events
from copenhagen.core.capability import CapabilitySpec
from copenhagen.core.plan import PlanIR
from copenhagen.core.recipe import RecipeSpec
from copenhagen.db.models import (
    ApiKey,
    Nonce,
    Plan,
    Principal,
    PublicationReview,
    Recipe,
    Role,
    Run,
    Session,
    StepRun,
    Tenant,
)
from copenhagen.db.store import connect, get, lock, put, rows, transaction
from copenhagen.engine.client import RunEngine, TemporalEngine
from copenhagen.engine.dispatch import Dispatcher, enqueue, outstanding, requeue
from copenhagen.engine.reconcile import Reconciler
from copenhagen.notify import build as build_notifier
from copenhagen.registry.publish import capability, catalog, publish, recipe, set_status
from copenhagen.service import Service
from copenhagen.settings import Settings

ROOT = Path(__file__).parent


def hook_secret(settings: Settings, source: str) -> str | None:
    """Each event source signs with its own secret so one vendor cannot forge another.

    ``COPENHAGEN_HOOK_SECRET_<SOURCE>`` wins. The shared ``HOOK_SECRET`` is accepted only
    outside production, for local development.
    """

    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", source):
        return None
    own = os.environ.get("COPENHAGEN_HOOK_SECRET_" + source.upper().replace("-", "_"))
    if own:
        return own
    return settings.hook_secret if settings.env != "prod" else None


def create_app(
    settings: Settings | None = None,
    *,
    db: Engine | None = None,
    run_engine: RunEngine | None = None,
) -> FastAPI:
    settings = settings or Settings()
    database = db or connect(settings.db_url)
    service = Service(database, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        delivery = Dispatcher(
            database,
            service.tenant,
            run_engine or await TemporalEngine.connect(settings),
            notifier=build_notifier(
                settings.notify_webhook_url,
                settings.notify_webhook_secret,
                allow_insecure=settings.env != "prod",
            ),
        )
        app.state.run_engine = delivery
        background = [asyncio.create_task(delivery.run())]
        lookup = getattr(delivery.engine, "status", None)
        if lookup is not None:
            reconciler = Reconciler(
                database,
                service.tenant,
                lookup,
                public_url=settings.public_url,
                interval=settings.reconcile_minutes * 60,
            )
            app.state.reconciler = reconciler
            background.append(asyncio.create_task(reconciler.run()))
        try:
            yield
        finally:
            for task in background:
                task.cancel()
            for task in background:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if db is None:
                database.dispose()

    app = FastAPI(title="Copenhagen", version="0.1.0", lifespan=lifespan)
    app.state.service = service
    app.add_middleware(SessionMiddleware, service=service)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "templates")
    oauth = OAuth()
    if settings.oidc_client_id:
        oauth.register(
            "company",
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            server_metadata_url=settings.oidc_metadata_url,
            client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
        )

    @app.exception_handler(ValueError)
    async def bad_request(request: Request, error: ValueError):
        if request.url.path.startswith("/v1/"):
            return JSONResponse({"detail": str(error)}, status_code=400)
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"message": str(error), "csrf": request.state.csrf},
            status_code=400,
        )

    def render(request: Request, name: str, **context: Any):
        return templates.TemplateResponse(
            request=request,
            name=name,
            context={
                "actor": request.state.actor,
                "csrf": request.state.csrf,
                "dev_login": settings.copenhagen_dev_login,
                **context,
            },
        )

    def fresh_login(request: Request) -> None:
        if not settings.copenhagen_dev_login and (
            request.state.authenticated_at is None
            or datetime.now(UTC) - request.state.authenticated_at > timedelta(minutes=15)
        ):
            raise ValueError("fresh OIDC sign-in required")

    def admin(request: Request) -> str:
        id_ = actor(request, "admin")
        with transaction(database) as session:
            if "admin" not in service.identity(session, id_).roles:
                raise HTTPException(403, "administrator role required")
        return id_

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        from sqlalchemy import text

        with database.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "environment": settings.env}

    @app.get("/auth/login")
    async def oidc_login(request: Request):
        if not settings.oidc_client_id:
            raise ValueError("configure Google OIDC or enable development login")
        return await oauth.company.authorize_redirect(
            request, settings.public_url + "/auth/callback", prompt="login", max_age=0
        )

    @app.get("/auth/callback")
    async def oidc_callback(request: Request):
        token = await oauth.company.authorize_access_token(request)
        info = token.get("userinfo") or await oauth.company.userinfo(token=token)
        if not info.get("email_verified") or info.get("hd") != settings.oidc_allowed_domain:
            raise ValueError("verified company-domain identity required")
        with transaction(database) as session:
            matches = [
                p
                for p in rows(session, Principal, service.tenant)
                if p.data.get("idp_subject") == info["sub"]
            ]
            if not matches:
                matches = [
                    p
                    for p in rows(session, Principal, service.tenant)
                    if p.status == "active"
                    and p.data.get("kind") == "user"
                    and not p.data.get("idp_subject")
                    and p.data.get("email", "").lower() == info["email"].lower()
                ]
                if len(matches) != 1:
                    raise ValueError("identity must be provisioned before sign-in")
                matches[0].data = {**matches[0].data, "idp_subject": info["sub"]}
            id_ = matches[0].id
            if matches[0].data["email"].lower() != info["email"].lower():
                raise ValueError("provisioned identity email does not match provider")
        login(service, request, id_)
        return RedirectResponse("/", status_code=303)

    if settings.copenhagen_dev_login:

        @app.post("/auth/dev")
        async def dev_login(request: Request):
            data = await request.form()
            login(service, request, str(data["principal_id"]))
            return RedirectResponse("/", status_code=303)

    @app.post("/auth/logout")
    def logout(request: Request):
        actor(request)
        with transaction(database) as session:
            row = session.get(Session, (service.tenant, request.state.session_hash))
            if row:
                row.status = "revoked"
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        with transaction(database) as session:
            latest = {}
            for item in rows(session, Recipe, service.tenant) if request.state.actor else []:
                if item.status == "active" and (
                    item.name not in latest or item.version > latest[item.name].version
                ):
                    latest[item.name] = item
            all_recipes = list(latest.values())
            recent = [
                r
                for r in rows(session, Run, service.tenant)
                if r.data["on_behalf_of"] == request.state.actor
            ][-10:]
            users = (
                [
                    p.id
                    for p in rows(session, Principal, service.tenant)
                    if p.data.get("kind") != "service"
                ]
                if settings.copenhagen_dev_login
                else []
            )
        return render(
            request, "home.html", recipes=all_recipes, runs=list(reversed(recent)), users=users
        )

    @app.get("/recipes/{name}", response_class=HTMLResponse)
    def recipe_form(request: Request, name: str):
        actor(request)
        with transaction(database) as session:
            spec = recipe(session, service.tenant, name)
        return render(request, "recipe.html", recipe=spec)

    @app.post("/recipes/{name}/preview", response_class=HTMLResponse)
    async def preview_form(request: Request, name: str):
        id_ = actor(request, "run")
        form = await request.form()
        with transaction(database) as session:
            spec = recipe(session, service.tenant, name, int(str(form["version"])))
        parameters = {}
        for key, schema in spec.parameters.items():
            value = form.get(key)
            if (value is None or value == "") and (
                "default" in schema or not schema.get("required", True)
            ):
                continue
            parameters[key] = (
                int(str(value))
                if schema["type"] == "integer"
                else value == "true"
                if schema["type"] == "boolean"
                else json.loads(str(value))
                if schema["type"] in {"object", "array", "null"}
                else str(value)
            )
        result = service.preview(id_, recipe_name=name, version=spec.version, parameters=parameters)
        return render(request, "preview.html", result=result)

    @app.post("/plans/{id_}/confirm")
    async def confirm_form(request: Request, id_: str):
        envelope = service.confirm(actor(request, "run"), id_)
        await app.state.run_engine.start(envelope)
        return RedirectResponse("/runs/" + envelope.run_id, status_code=303)

    @app.get("/runs/{id_}", response_class=HTMLResponse)
    def run_page(request: Request, id_: str):
        return render(request, "run.html", run=service.read_run(actor(request), id_))

    @app.post("/runs/{id_}/steps/{step}/{action_name}")
    async def recovery_form(request: Request, id_: str, step: str, action_name: str):
        if action_name not in {"retry", "skip"}:
            raise ValueError("invalid recovery action")
        form = await request.form()
        reason = str(form.get("reason", ""))
        service.action(actor(request, "run"), id_, step, action_name, reason)
        await app.state.run_engine.signal(
            id_, "step_action", {"step_id": step, "action": action_name, "reason": reason}
        )
        return RedirectResponse("/runs/" + id_, status_code=303)

    @app.post("/runs/{id_}/cancel")
    async def cancel_form(request: Request, id_: str):
        form = await request.form()
        service.action(
            actor(request, "run"), id_, None, "cancel", compensate=form.get("compensate") == "true"
        )
        await app.state.run_engine.signal(id_, "cancel", form.get("compensate") == "true")
        return RedirectResponse("/runs/" + id_, status_code=303)

    @app.get("/approvals", response_class=HTMLResponse)
    def approvals_page(request: Request):
        return render(request, "approvals.html", items=service.inbox(actor(request)))

    @app.post("/approvals/{id_}/decide")
    async def decide_form(request: Request, id_: str):
        form = await request.form()
        edits = json.loads(str(form["edits"])) if form.get("edits") else None
        result = service.decide(
            actor(request, "approve"),
            id_,
            form["decision"] == "approve",
            str(form["reason"]),
            edits=edits,
            authenticated_at=request.state.authenticated_at,
        )
        await app.state.run_engine.signal(result["run_id"], "approval_decided", result)
        return RedirectResponse("/approvals", status_code=303)

    @app.get("/tasks", response_class=HTMLResponse)
    def tasks_page(request: Request):
        return render(request, "tasks.html", items=service.inbox(actor(request), tasks=True))

    @app.post("/tasks/{id_}/complete")
    async def task_form(request: Request, id_: str):
        form = await request.form()
        result = service.complete_task(actor(request, "run"), id_, json.loads(str(form["outputs"])))
        await app.state.run_engine.signal(result["run_id"], "task_completed", result)
        return RedirectResponse("/tasks", status_code=303)

    @app.post("/recipes/{name}/{version}/preapprove")
    def preapprove_form(request: Request, name: str, version: int):
        preapprove_api(request, name, version)
        return RedirectResponse("/recipes/" + name, status_code=303)

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request):
        admin(request)
        with transaction(database) as session:
            tenant = session.get(Tenant, service.tenant)
            assert tenant is not None
            return render(
                request,
                "admin.html",
                principals=rows(session, Principal, service.tenant),
                roles=rows(session, Role, service.tenant),
                config=tenant.config,
            )

    @app.post("/admin/principals")
    async def principal_form(request: Request):
        form = await request.form()
        provision(
            service,
            admin(request),
            PrincipalInput.model_validate(
                {"id": form["id"], "email": form["email"], "kind": form["kind"]}
            ),
        )
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/roles")
    async def role_form(request: Request):
        form = await request.form()
        define_role(
            service,
            admin(request),
            RoleInput(
                name=str(form["name"]),
                patterns=[p.strip() for p in str(form.get("patterns", "")).split(",") if p.strip()],
            ),
        )
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/grants")
    async def grant_form(request: Request):
        form = await request.form()
        service.assign_role(
            admin(request), str(form["principal"]), str(form["role"]), form["enabled"] == "true"
        )
        return RedirectResponse("/admin", status_code=303)

    @app.post("/admin/config")
    async def config_form(request: Request):
        form = await request.form()
        configure(service, admin(request), TenantConfig.model_validate_json(str(form["config"])))
        return RedirectResponse("/admin", status_code=303)

    @app.get("/reviews", response_class=HTMLResponse)
    def reviews_page(request: Request):
        return render(request, "reviews.html", reviews=reviews_api(request))

    @app.post("/reviews/{id_}/decide")
    async def review_form(request: Request, id_: str):
        form = await request.form()
        review_decide_api(
            request, id_, {"approved": form["decision"] == "approve", "reason": str(form["reason"])}
        )
        return RedirectResponse("/reviews", status_code=303)

    @app.get("/catalog", response_class=HTMLResponse)
    def catalog_page(request: Request, q: str = ""):
        actor(request)
        with transaction(database) as session:
            specs = catalog(session, service.tenant, q)
        return render(request, "catalog.html", specs=specs, q=q)

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request):
        result = audit_api(request)
        return render(request, "audit.html", events=result)

    @app.get("/v1/capabilities")
    def capabilities_api(request: Request, q: str = ""):
        actor(request)
        with transaction(database) as session:
            return catalog(session, service.tenant, q)

    @app.get("/v1/capabilities/{name}/{version}")
    def capability_api(request: Request, name: str, version: int):
        actor(request)
        with transaction(database) as session:
            return capability(session, service.tenant, f"{name}@{version}").model_dump(
                mode="json", by_alias=True
            )

    @app.get("/v1/capabilities/{name}")
    def latest_capability_api(request: Request, name: str):
        actor(request)
        with transaction(database) as session:
            matches = [s for s in catalog(session, service.tenant) if s["name"] == name]
        if not matches:
            raise HTTPException(404, "capability not found")
        return max(matches, key=lambda s: s["version"])

    @app.post("/v1/capabilities")
    def publish_capability_api(request: Request, body: dict[str, Any]):
        id_ = actor(request, "admin")
        with transaction(database) as session:
            identity = service.identity(session, id_)
            if not {"admin", "capability_author"} & set(identity.roles):
                raise ValueError("capability author role required")
            spec = CapabilitySpec.model_validate(body["spec"])
            reviewer = body.get("second_reviewer")
            # A caller-supplied name is not evidence of an independent review.
            if spec.risk.class_ in {"financial", "identity", "infrastructure", "destructive"}:
                from copenhagen.registry.review import propose

                return {"published": False, "review_id": propose(service, id_, spec)}
            changed = publish(session, service.tenant, spec, id_, reviewer)
        return {"published": changed}

    @app.get("/v1/publication-reviews")
    def reviews_api(request: Request):
        id_ = actor(request)
        with transaction(database) as session:
            if not {"admin", "capability_author"} & set(service.identity(session, id_).roles):
                raise HTTPException(403, "publishing authority required")
            return [
                {"id": r.id, "status": r.status, **r.data}
                for r in rows(session, PublicationReview, service.tenant)
            ]

    @app.post("/v1/publication-reviews/{id_}/decide")
    def review_decide_api(request: Request, id_: str, body: dict[str, Any]):
        from copenhagen.registry.review import decide

        if type(body.get("approved")) is not bool:
            raise ValueError("approved must be boolean")
        fresh_login(request)
        decide(service, actor(request, "admin"), id_, body["approved"], body.get("reason", ""))
        return {"accepted": True}

    @app.post("/v1/capabilities/{name}/{version}/status")
    def capability_status_api(request: Request, name: str, version: int, body: dict[str, Any]):
        id_ = admin(request)
        with transaction(database) as session:
            set_status(session, service.tenant, f"{name}@{version}", body["status"], id_)
        return {"updated": True}

    @app.get("/v1/recipes")
    def recipes_api(request: Request):
        actor(request)
        with transaction(database) as session:
            return [
                {"ref": r.id, **r.data}
                for r in rows(session, Recipe, service.tenant)
                if r.status == "active"
            ]

    @app.post("/v1/recipes")
    def publish_recipe_api(request: Request, body: dict[str, Any]):
        id_ = admin(request)
        with transaction(database) as session:
            return {
                "published": publish(session, service.tenant, RecipeSpec.model_validate(body), id_)
            }

    @app.post("/v1/recipes/{name}/preview")
    def recipe_preview_api(request: Request, name: str, body: dict[str, Any]):
        return service.preview(
            actor(request, "run"),
            recipe_name=name,
            version=body.get("version"),
            parameters=body.get("parameters", {}),
        )

    @app.post("/v1/recipes/{name}/run")
    async def recipe_run_api(request: Request, name: str, body: dict[str, Any]):
        id_ = actor(request, "run")
        envelope = service.run_recipe(
            id_,
            name,
            body.get("parameters", {}),
            body.get("version"),
            request.headers.get("idempotency-key"),
        )
        await app.state.run_engine.start(envelope)
        return {"run_id": envelope.run_id}

    @app.post("/v1/plans")
    def plan_api(request: Request, body: PlanIR):
        return service.preview(actor(request, "run"), plan=body)

    @app.get("/v1/plans/{id_}")
    def get_plan_api(request: Request, id_: str):
        id_actor = actor(request)
        with transaction(database) as session:
            item = get(session, Plan, service.tenant, id_)
            if item.data["requester_id"] != id_actor:
                raise ValueError("plan belongs to another requester")
            return {"id": item.id, **item.data}

    @app.post("/v1/plans/{id_}/confirm")
    async def confirm_api(request: Request, id_: str):
        envelope = service.confirm(actor(request, "run"), id_)
        await app.state.run_engine.start(envelope)
        return {"run_id": envelope.run_id}

    @app.get("/v1/runs/{id_}")
    def run_api(request: Request, id_: str):
        return service.read_run(actor(request), id_)

    @app.get("/v1/runs")
    def runs_api(request: Request):
        id_ = actor(request)
        with transaction(database) as session:
            return [
                {"id": r.id, "status": r.status, **r.data}
                for r in rows(session, Run, service.tenant)
                if r.data["on_behalf_of"] == id_
            ]

    @app.post("/v1/runs/{id_}/cancel")
    async def cancel_api(request: Request, id_: str, body: dict[str, Any]):
        service.action(
            actor(request, "run"),
            id_,
            None,
            "cancel",
            body.get("reason", ""),
            compensate=body.get("compensate") is True,
        )
        await app.state.run_engine.signal(id_, "cancel", bool(body.get("compensate", False)))
        return {"cancel_requested": True}

    @app.post("/v1/runs/{id_}/steps/{step}/{action_name}")
    async def action_api(
        request: Request, id_: str, step: str, action_name: str, body: dict[str, Any]
    ):
        if action_name not in {"retry", "skip"}:
            raise ValueError("recovery action must be retry or skip")
        reason = body.get("reason", "")
        service.action(actor(request, "run"), id_, step, action_name, reason)
        await app.state.run_engine.signal(
            id_, "step_action", {"step_id": step, "action": action_name, "reason": reason}
        )
        return {"accepted": True}

    @app.get("/v1/approvals")
    def approvals_api(request: Request, mine: bool = True):
        return service.inbox(actor(request))

    @app.post("/v1/approvals/{id_}/decide")
    async def decide_api(request: Request, id_: str, body: dict[str, Any]):
        if type(body.get("approved")) is not bool:
            raise ValueError("approved must be a boolean")
        result = service.decide(
            actor(request, "approve"),
            id_,
            body["approved"],
            body.get("reason", ""),
            edits=body.get("edits"),
            authenticated_at=request.state.authenticated_at,
        )
        await app.state.run_engine.signal(result["run_id"], "approval_decided", result)
        return {"accepted": True}

    @app.get("/v1/tasks")
    def tasks_api(request: Request):
        return service.inbox(actor(request), tasks=True)

    @app.post("/v1/tasks/{id_}/complete")
    async def complete_task_api(request: Request, id_: str, body: dict[str, Any]):
        result = service.complete_task(actor(request, "run"), id_, body["outputs"])
        await app.state.run_engine.signal(result["run_id"], "task_completed", result)
        return {"accepted": True}

    @app.post("/v1/recipes/{name}/{version}/preapprove")
    def preapprove_api(request: Request, name: str, version: int):
        if not settings.copenhagen_dev_login and (
            request.state.authenticated_at is None
            or datetime.now(UTC) - request.state.authenticated_at > timedelta(minutes=15)
        ):
            raise ValueError("fresh OIDC sign-in required for recipe pre-approval")
        return {"preapproval_id": service.preapprove(actor(request, "approve"), name, version)}

    @app.get("/v1/audit")
    def audit_api(request: Request, run_id: str | None = None):
        id_ = actor(request)
        with transaction(database) as session:
            identity = service.identity(session, id_)
            if not {"auditor", "admin"} & set(identity.roles):
                if not run_id:
                    raise HTTPException(403, "auditor role required for tenant-wide audit")
                service.read_run(id_, run_id)
            return [
                {**e.data, "prev_hash": e.prev_hash, "hash": e.hash}
                for e in events(session, service.tenant)
                if run_id is None or e.data.get("run_id") == run_id
            ]

    def mint_key(request: Request, scopes: list[str], hours: int) -> str:
        id_ = actor(request, "run")
        if not set(scopes) <= request.state.scopes:
            raise HTTPException(403, "new key cannot exceed the current credential scopes")
        return service.issue_key(
            id_, scopes, hours, not_after=getattr(request.state, "key_expires_at", None)
        )

    @app.post("/v1/api-keys/{key_id}/revoke")
    def revoke_key_api(request: Request, key_id: str):
        from copenhagen.audit.chain import append
        from copenhagen.db.store import new_id

        id_ = actor(request, "run")
        is_admin = "admin" in request.state.scopes and _is_admin(id_)
        with transaction(database) as session:
            matches = [
                k
                for k in rows(session, ApiKey, service.tenant)
                if k.data["key_id"] == key_id and (is_admin or k.data["principal_id"] == id_)
            ]
            if not matches:
                raise HTTPException(404, "API key not found")
            matches[0].status = "revoked"
            append(
                session,
                service.tenant,
                new_id("key"),
                "api_key.revoked",
                principal_id=id_,
                key_id=key_id,
            )
        return {"revoked": True}

    def _is_admin(id_: str) -> bool:
        with transaction(database) as session:
            return "admin" in service.identity(session, id_).roles

    @app.get("/v1/api-keys")
    def list_keys_api(request: Request):
        """Key metadata only; hashes and secrets never leave the database."""

        id_ = actor(request, "read")
        is_admin = "admin" in request.state.scopes and _is_admin(id_)
        with transaction(database) as session:
            return [
                {
                    "key_id": k.data["key_id"],
                    "principal_id": k.data["principal_id"],
                    "scopes": k.data["scopes"],
                    "expires_at": k.data["expires_at"],
                    "status": k.status,
                }
                for k in rows(session, ApiKey, service.tenant)
                if is_admin or k.data["principal_id"] == id_
            ]

    @app.post("/v1/api-keys")
    def key_api(request: Request, body: dict[str, Any]):
        return {"key": mint_key(request, body["scopes"], body.get("hours", 24))}

    @app.post("/v1/principals/{target}/roles/{role}")
    def role_api(request: Request, target: str, role: str, body: dict[str, Any]):
        service.assign_role(admin(request), target, role, body["enabled"])
        return {"updated": True}

    @app.get("/v1/admin/principals")
    def principals_api(request: Request):
        admin(request)
        with transaction(database) as session:
            return [
                {"id": p.id, "status": p.status, **p.data}
                for p in rows(session, Principal, service.tenant)
            ]

    @app.post("/v1/admin/principals")
    def provision_api(request: Request, body: PrincipalInput):
        provision(service, admin(request), body)
        return {"created": True}

    @app.post("/v1/admin/principals/{target}/disable")
    def disable_api(request: Request, target: str):
        disable_principal(service, admin(request), target)
        return {"disabled": True}

    @app.post("/v1/admin/roles")
    def define_role_api(request: Request, body: RoleInput):
        define_role(service, admin(request), body)
        return {"updated": True}

    @app.get("/v1/admin/outbox")
    def outbox_api(request: Request, status: str = "dead"):
        admin(request)
        if status not in {"dead", "pending"}:
            raise HTTPException(400, "status must be dead or pending")
        with transaction(database) as session:
            return outstanding(session, service.tenant, status)

    @app.post("/v1/admin/outbox/{id_}/requeue")
    def requeue_api(request: Request, id_: str):
        actor = admin(request)
        try:
            with transaction(database) as session:
                requeue(session, service.tenant, id_, actor)
        except LookupError as error:
            raise HTTPException(404, str(error)) from error
        return {"requeued": True}

    @app.post("/v1/admin/reconcile")
    async def reconcile_api(request: Request):
        admin(request)
        reconciler = getattr(request.app.state, "reconciler", None)
        if reconciler is None:
            raise HTTPException(409, "run engine does not support reconciliation")
        return {"closed": await reconciler.once()}

    @app.get("/v1/admin/config")
    def get_config_api(request: Request):
        admin(request)
        with transaction(database) as session:
            tenant = session.get(Tenant, service.tenant)
            return tenant.config if tenant else {}

    @app.put("/v1/admin/config")
    def configure_api(request: Request, body: TenantConfig):
        configure(service, admin(request), body)
        return {"updated": True}

    @app.post("/v1/callbacks/{run_id}/{step_id}")
    async def callback_api(request: Request, run_id: str, step_id: str):
        from copenhagen.core.schema import validate_values

        if not settings.callback_secret:
            raise HTTPException(503, "callback signing key not configured")
        raw = await request.body()
        timestamp = request.headers.get("x-copenhagen-timestamp", "")
        nonce = request.headers.get("x-copenhagen-nonce", "")
        signature = request.headers.get("x-copenhagen-signature", "")
        if (
            not timestamp.isdigit()
            or abs(datetime.now(UTC).timestamp() - int(timestamp)) > 300
            or not 16 <= len(nonce) <= 128
        ):
            raise HTTPException(401, "invalid callback timestamp or nonce")
        payload = f"{run_id}.{step_id}.{timestamp}.{nonce}.".encode() + raw
        expected = hmac.new(settings.callback_secret.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(401, "invalid callback signature")
        body = json.loads(raw)
        if not isinstance(body, dict) or not isinstance(body.get("outputs"), dict):
            raise ValueError("callback requires job_id and outputs")
        with transaction(database) as session:
            message_id = "callback:" + nonce
            lock(session, f"callback:{service.tenant}:{nonce}")
            previous = session.get(Nonce, (service.tenant, message_id))
            fingerprint = hashlib.sha256(payload).hexdigest()
            if previous:
                if previous.data["fingerprint"] != fingerprint:
                    raise HTTPException(409, "callback nonce reused")
            else:
                run = get(session, Run, service.tenant, run_id)
                if run.status in {
                    "succeeded",
                    "failed",
                    "cancelled",
                    "compensated",
                    "succeeded_with_skips",
                }:
                    raise HTTPException(409, "run has finished")
                step = get(session, StepRun, service.tenant, f"{run_id}:{step_id}")
                if not step.data.get("handle") or step.data["handle"] != body.get("job_id"):
                    raise HTTPException(
                        409, "job is not awaiting this callback; retry after registration"
                    )
                cap = capability(session, service.tenant, step.data["capability"])
                if cap.executor.mode != "webhook_callback":
                    raise ValueError("capability does not accept callbacks")
                validate_values(cap.outputs, body["outputs"])
                put(session, Nonce, service.tenant, message_id, {"fingerprint": fingerprint})
                enqueue(
                    session,
                    service.tenant,
                    message_id,
                    run_id,
                    "backend_completed",
                    {
                        "id": message_id,
                        "step_id": step_id,
                        "handle": body["job_id"],
                        "outputs": body["outputs"],
                    },
                )
        await app.state.run_engine.signal(run_id, "backend_completed", {})
        return {"accepted": True}

    @app.post("/v1/hooks/{source}")
    async def hook_api(request: Request, source: str):
        secret = hook_secret(settings, source)
        if not secret:
            raise HTTPException(503, "hook signing key not configured for this source")
        raw = await request.body()
        timestamp = request.headers.get("x-copenhagen-timestamp", "")
        nonce = request.headers.get("x-copenhagen-nonce", "")
        signature = request.headers.get("x-copenhagen-signature", "")
        if (
            not timestamp.isdigit()
            or abs(datetime.now(UTC).timestamp() - int(timestamp)) > 300
            or not 16 <= len(nonce) <= 128
        ):
            raise HTTPException(401, "invalid hook timestamp or nonce")
        payload = source.encode() + b"." + timestamp.encode() + b"." + nonce.encode() + b"." + raw
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(401, "invalid hook signature")
        body = json.loads(raw)
        if not isinstance(body, dict) or not isinstance(body.get("recipe"), str):
            raise ValueError("hook requires a recipe name and parameters object")
        with transaction(database) as session:
            lock(session, f"hook:{service.tenant}:{nonce}")
            if session.get(Nonce, (service.tenant, nonce)):
                raise HTTPException(409, "hook nonce already used")
            tenant = session.get(Tenant, service.tenant)
            assert tenant is not None
            config = tenant.config.get("hooks", {}).get(source)
            if not config or body["recipe"] not in config.get("allowed_recipes", []):
                raise ValueError("hook source is not allowed to run this recipe")
            id_ = config["principal_id"]
            identity = service.identity(session, id_)
            if identity.kind != "service":
                raise ValueError("hook identity must be a service principal")
            spec = recipe(session, service.tenant, body["recipe"], body.get("version"))
            from copenhagen.policy.preapproval import valid_preapproval

            pre = valid_preapproval(session, service.tenant, spec, body.get("parameters", {}), id_)
            if not spec.allow_event_trigger or pre is None:
                raise ValueError("event triggers require an eligible pre-approved recipe (I12)")
            if set(spec.approval.covers_steps) != {s.id for s in spec.steps}:
                raise ValueError("event-triggered recipe must cover every step")
            result = service.preview(
                id_,
                recipe_name=spec.name,
                version=spec.version,
                parameters=body.get("parameters", {}),
                _session=session,
            )
            envelope = service.confirm(id_, result["id"], _session=session)
            put(
                session,
                Nonce,
                service.tenant,
                nonce,
                {"source": source, "timestamp": timestamp, "run_id": envelope.run_id},
            )
        await app.state.run_engine.start(envelope)
        return {"run_id": envelope.run_id}

    return app
