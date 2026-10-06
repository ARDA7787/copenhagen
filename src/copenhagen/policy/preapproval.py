"""Recipe signatures remain valid only for the exact version and constraint set."""

from typing import Any

from sqlalchemy.orm import Session

from copenhagen.audit.chain import append
from copenhagen.core.canonical import sha256_hex
from copenhagen.core.conditions import Truth, evaluate_all
from copenhagen.core.identity import Principal
from copenhagen.core.recipe import RecipeSpec
from copenhagen.db.models import Preapproval, Recipe, Tenant
from copenhagen.db.store import lock, new_id, rows
from copenhagen.policy.identity import principal
from copenhagen.registry.publish import capability


def eligible_roles(session: Session, tenant: str, recipe: RecipeSpec) -> set[str]:
    required: set[str] = set()
    for step in recipe.steps:
        if step.id in recipe.approval.covers_steps:
            cap = capability(session, tenant, step.capability)
            if cap.status != "active" or cap.risk.class_ == "destructive":
                raise ValueError("disabled/destructive capability cannot be pre-approved")
            required.add(cap.approval.approver_role or f"approver:{cap.executor.queue}")
    return required


def preapprove(session: Session, tenant: str, recipe: RecipeSpec, actor: Principal) -> str:
    lock(session, f"catalog:{tenant}")
    if not recipe.approval.pre_approvable or not recipe.approval.covers_steps:
        raise ValueError("recipe does not support pre-approval")
    if not eligible_roles(session, tenant, recipe) <= set(actor.roles):
        raise ValueError("pre-approver must hold every covered domain approver role")
    latest = max(r.version for r in rows(session, Recipe, tenant) if r.name == recipe.name)
    if latest != recipe.version:
        raise ValueError("only the latest recipe version can be pre-approved")
    id_ = new_id("preapproval")
    constraint_hash = sha256_hex(recipe.approval.model_dump(mode="json"))
    session.add(
        Preapproval(
            tenant_id=tenant,
            id=id_,
            status="active",
            data={
                "recipe_name": recipe.name,
                "recipe_version": recipe.version,
                "approved_by": actor.id,
                "constraints_hash": constraint_hash,
                "capabilities": [s.capability for s in recipe.steps],
            },
        )
    )
    append(
        session,
        tenant,
        id_,
        "recipe.preapproved",
        principal_id=actor.id,
        recipe_ref=recipe.ref,
        constraints_hash=constraint_hash,
    )
    return id_


def valid_preapproval(
    session: Session, tenant: str, recipe: RecipeSpec, parameters: dict[str, Any], requester_id: str
) -> Preapproval | None:
    if not recipe.approval.pre_approvable:
        return None
    tenant_row = session.get(Tenant, tenant)
    if tenant_row is None:
        raise ValueError("tenant not found")
    config = tenant_row.config
    domains = frozenset(config.get("internal_domains", []))
    if (
        evaluate_all(recipe.approval.constraints, parameters, internal_domains=domains)
        is not Truth.TRUE
    ):
        return None
    latest = max(r.version for r in rows(session, Recipe, tenant) if r.name == recipe.name)
    if latest != recipe.version:
        return None
    for item in reversed(rows(session, Preapproval, tenant)):
        data = item.data
        if (
            item.status != "active"
            or data["recipe_name"] != recipe.name
            or data["recipe_version"] != recipe.version
        ):
            continue
        if data["approved_by"] == requester_id:
            continue
        if data["constraints_hash"] != sha256_hex(recipe.approval.model_dump(mode="json")):
            continue
        approver = principal(session, tenant, data["approved_by"])
        if approver.status != "active" or not eligible_roles(session, tenant, recipe) <= set(
            approver.roles
        ):
            continue
        return item
    return None
