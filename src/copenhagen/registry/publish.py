"""Versioned, immutable capability and recipe catalog."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from copenhagen.audit.chain import append
from copenhagen.core.capability import HIGH_RISK, CapabilitySpec
from copenhagen.core.recipe import RecipeSpec
from copenhagen.db.models import Capability, Preapproval, Recipe
from copenhagen.db.store import lock


def capability(session: Session, tenant: str, ref: str) -> CapabilitySpec:
    name, version = ref.rsplit("@", 1)
    row = session.scalar(
        select(Capability).where(
            Capability.tenant_id == tenant,
            Capability.name == name,
            Capability.version == int(version),
        )
    )
    if row is None:
        raise ValueError(f"capability {ref} does not exist")
    return CapabilitySpec.model_validate({**row.data, "status": row.status})


def recipe(session: Session, tenant: str, name: str, version: int | None = None) -> RecipeSpec:
    query = select(Recipe).where(Recipe.tenant_id == tenant, Recipe.name == name)
    if version is not None:
        query = query.where(Recipe.version == version)
    row = session.scalar(query.order_by(Recipe.version.desc()).limit(1))
    if row is None or row.status != "active":
        raise ValueError(f"recipe {name} is unavailable")
    return RecipeSpec.model_validate(row.data)


def publish(
    session: Session,
    tenant: str,
    spec: CapabilitySpec | RecipeSpec,
    actor: str,
    second_reviewer: str | None = None,
) -> bool:
    lock(session, f"catalog:{tenant}")
    model = Capability if isinstance(spec, CapabilitySpec) else Recipe
    data = spec.model_dump(mode="json", by_alias=True)
    existing = session.get(model, (tenant, spec.ref))
    if existing:
        if existing.data != data:
            raise ValueError("published version is immutable; increment the version")
        return False
    if (
        isinstance(spec, CapabilitySpec)
        and spec.risk.class_ in HIGH_RISK
        and (not second_reviewer or second_reviewer == actor)
    ):
        raise ValueError("high-risk publication needs an independent second reviewer")
    if isinstance(spec, RecipeSpec):
        for step in spec.steps:
            if capability(session, tenant, step.capability).status != "active":
                raise ValueError("recipes require active pinned capabilities")
        void_preapprovals(session, tenant, recipe_name=spec.name, reason="new recipe version")
    session.add(
        model(
            tenant_id=tenant,
            id=spec.ref,
            name=spec.name,
            version=spec.version,
            status=getattr(spec, "status", "active"),
            data=data,
        )
    )
    append(
        session,
        tenant,
        f"publish:{spec.ref}",
        f"{spec.kind.lower()}.published",
        principal_id=actor,
        capability=spec.ref,
        second_reviewer=second_reviewer,
    )
    return True


def void_preapprovals(
    session: Session,
    tenant: str,
    *,
    recipe_name: str | None = None,
    cap_ref: str | None = None,
    reason: str,
) -> None:
    for item in session.scalars(
        select(Preapproval).where(Preapproval.tenant_id == tenant, Preapproval.status == "active")
    ):
        if recipe_name == item.data["recipe_name"] or cap_ref in item.data.get("capabilities", []):
            item.status = "voided"
            item.data = {**item.data, "voided_reason": reason}
            append(
                session,
                tenant,
                f"void:{item.id}",
                "recipe.preapproval_voided",
                preapproval_id=item.id,
                reason=reason,
            )


def set_status(session: Session, tenant: str, ref: str, status: str, actor: str) -> None:
    if status not in {"active", "deprecated", "disabled"}:
        raise ValueError("invalid status")
    lock(session, f"catalog:{tenant}")
    item = session.get(Capability, (tenant, ref))
    if item is None:
        raise ValueError("capability not found")
    if item.status == status:
        return
    item.status = status
    if status != "active":
        void_preapprovals(session, tenant, cap_ref=ref, reason=f"capability {status}")
    from copenhagen.db.store import new_id

    append(
        session,
        tenant,
        new_id("evt"),
        "capability.status_changed",
        principal_id=actor,
        capability=ref,
        status=status,
    )


def catalog(session: Session, tenant: str, q: str = "") -> list[dict[str, Any]]:
    query = select(Capability).where(Capability.tenant_id == tenant)
    if q:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            from sqlalchemy import func

            query = query.where(
                func.to_tsvector(
                    "simple", Capability.name + " " + Capability.data["summary"].as_string()
                ).op("@@")(func.plainto_tsquery("simple", q))
            )
        else:
            query = query.where(Capability.name.contains(q))
    return [
        {**row.data, "status": row.status}
        for row in session.scalars(query.order_by(Capability.name, Capability.version))
    ]
