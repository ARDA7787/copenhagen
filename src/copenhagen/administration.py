"""Company provisioning independent of development fixtures and example runbooks."""

from typing import Literal

from pydantic import EmailStr, Field

from copenhagen.audit.chain import append
from copenhagen.core.capability import RiskClass, Spec
from copenhagen.db.models import Principal, PrincipalRole, Role, Tenant
from copenhagen.db.store import get, lock, new_id, put, rows, transaction
from copenhagen.service import Service


class PrincipalInput(Spec, frozen=True):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    email: EmailStr
    kind: Literal["user", "service"] = "user"
    idp_subject: str | None = None


class RoleInput(Spec, frozen=True):
    name: str = Field(pattern=r"^[a-z][a-z0-9_:-]{0,100}$")
    patterns: list[str] = Field(default_factory=list)


class TenantConfig(Spec, frozen=True):
    internal_domains: list[str] = Field(default_factory=list)
    kill_switch: bool = False
    money_ceiling: int = Field(default=500000, ge=0, strict=True)
    daily_money_cents: int = Field(default=1000000, ge=0, strict=True)
    daily_messages: int = Field(default=1000, ge=0, strict=True)
    daily_records: int = Field(default=10000, ge=0, strict=True)
    hooks: dict[str, "HookConfig"] = Field(default_factory=dict)
    # Minimum risk class by capability name pattern, e.g. ``{"payments.*": "financial"}``.
    risk_floors: dict[str, RiskClass] = Field(default_factory=dict)
    # Extra risk classes whose publication needs an independent second reviewer. High-risk
    # classes always need one; this list can only add to them.
    review_required: list[RiskClass] = Field(default_factory=list)


class HookConfig(Spec, frozen=True):
    principal_id: str
    allowed_recipes: list[str]


def initialize(service: Service, name: str, admin: PrincipalInput) -> None:
    if admin.kind != "user":
        raise ValueError("initial administrator must be a human")
    with transaction(service.engine) as session:
        lock(session, f"initialize:{service.tenant}")
        if session.get(Tenant, service.tenant):
            raise ValueError("workspace already initialized; use administration commands")
        session.add(
            Tenant(
                id=service.tenant,
                name=name,
                config=TenantConfig(
                    internal_domains=[str(admin.email).rsplit("@", 1)[1]]
                ).model_dump(mode="json"),
            )
        )
        put(session, Principal, service.tenant, admin.id, admin.model_dump(mode="json"))
        for role, patterns in (
            ("admin", ["*"]),
            ("requester", []),
            ("capability_author", []),
            ("auditor", []),
        ):
            put(session, Role, service.tenant, role, {"patterns": patterns})
        put(
            session,
            PrincipalRole,
            service.tenant,
            f"{admin.id}:admin",
            {"principal_id": admin.id, "role": "admin"},
        )
        append(
            session,
            service.tenant,
            "workspace:initialized",
            "workspace.initialized",
            principal_id=admin.id,
            name=name,
        )


def require_admin(service: Service, session, actor: str) -> None:
    identity = service.identity(session, actor)
    if identity.status != "active" or "admin" not in identity.roles:
        raise ValueError("active administrator role required")


def provision(service: Service, actor: str, spec: PrincipalInput) -> None:
    with transaction(service.engine) as session:
        require_admin(service, session, actor)
        lock(session, f"identities:{service.tenant}")
        if session.get(Principal, (service.tenant, spec.id)):
            raise ValueError("principal already exists")
        if any(
            p.data.get("email", "").lower() == str(spec.email).lower()
            or (spec.idp_subject and p.data.get("idp_subject") == spec.idp_subject)
            for p in rows(session, Principal, service.tenant)
        ):
            raise ValueError("identity already provisioned")
        put(session, Principal, service.tenant, spec.id, spec.model_dump(mode="json"))
        append(
            session,
            service.tenant,
            new_id("principal"),
            "principal.provisioned",
            principal_id=actor,
            target=spec.id,
            kind=spec.kind,
        )


def define_role(service: Service, actor: str, spec: RoleInput) -> None:
    with transaction(service.engine) as session:
        require_admin(service, session, actor)
        if spec.name == "admin" and spec.patterns != ["*"]:
            raise ValueError("the built-in administrator role cannot be redefined")
        put(session, Role, service.tenant, spec.name, {"patterns": spec.patterns})
        append(
            session,
            service.tenant,
            new_id("role"),
            "role.defined",
            principal_id=actor,
            role=spec.name,
            patterns=spec.patterns,
        )


def configure(service: Service, actor: str, config: TenantConfig) -> None:
    with transaction(service.engine) as session:
        require_admin(service, session, actor)
        for hook in config.hooks.values():
            if service.identity(session, hook.principal_id).kind != "service":
                raise ValueError("hook identity must be a service principal")
        tenant = session.get(Tenant, service.tenant)
        assert tenant is not None
        tenant.config = config.model_dump(mode="json")
        append(
            session,
            service.tenant,
            new_id("config"),
            "workspace.configured",
            principal_id=actor,
            config=tenant.config,
        )


def disable_principal(service: Service, actor: str, target: str) -> None:
    with transaction(service.engine) as session:
        require_admin(service, session, actor)
        if actor == target:
            raise ValueError("use another administrator to disable your own account")
        get(session, Principal, service.tenant, target).status = "disabled"
        append(
            session,
            service.tenant,
            new_id("principal"),
            "principal.disabled",
            principal_id=actor,
            target=target,
        )
