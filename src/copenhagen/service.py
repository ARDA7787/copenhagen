"""Application transactions shared by API and CLI; permissions are enforced here."""

from __future__ import annotations

import hashlib
import secrets
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from copenhagen.audit.chain import append
from copenhagen.core.canonical import inputs_hash
from copenhagen.core.capability import HIGH_RISK
from copenhagen.core.identity import Principal, RunContext
from copenhagen.core.plan import PlanIR
from copenhagen.core.recipe import instantiate
from copenhagen.core.schema import validate_values
from copenhagen.db.models import (
    ApiKey,
    Approval,
    HumanTask,
    Nonce,
    Outbox,
    Plan,
    PrincipalRole,
    Role,
    Run,
    StepRun,
    Tenant,
)
from copenhagen.db.store import get, lock, new_id, put, rows, transaction
from copenhagen.engine.contracts import RunEnvelope
from copenhagen.engine.dispatch import enqueue
from copenhagen.notify import notification
from copenhagen.policy.engine import PolicyEngine
from copenhagen.policy.identity import principal
from copenhagen.policy.preapproval import preapprove, valid_preapproval
from copenhagen.registry.publish import capability, recipe
from copenhagen.settings import Settings
from copenhagen.validator.validate import validate


class Service:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings
        self.tenant = settings.tenant_id
        self.policy = PolicyEngine(directory=settings.policy_directory)

    def identity(self, session: Session, id_: str) -> Principal:
        return principal(session, self.tenant, id_)

    def context(self, session: Session) -> RunContext:
        tenant = session.get(Tenant, self.tenant)
        if tenant is None:
            raise ValueError("tenant is not initialized; run copenhagen init")
        return RunContext(
            env=self.settings.env,
            internal_domains=frozenset(tenant.config.get("internal_domains", [])),
            kill_switch=tenant.config.get("kill_switch", False),
            money_ceiling=tenant.config.get("money_ceiling", 500000),
        )

    def preview(
        self,
        actor: str,
        *,
        recipe_name: str | None = None,
        version: int | None = None,
        parameters: dict[str, Any] | None = None,
        plan: PlanIR | None = None,
        _session: Session | None = None,
    ) -> dict[str, Any]:
        with nullcontext(_session) if _session is not None else transaction(self.engine) as session:
            identity = self.identity(session, actor)
            spec = recipe(session, self.tenant, recipe_name, version) if recipe_name else None
            if spec:
                parameters = {
                    **{k: v["default"] for k, v in spec.parameters.items() if "default" in v},
                    **(parameters or {}),
                }
                plan = instantiate(spec, parameters)
            if plan is None:
                raise ValueError("a recipe or plan is required")
            caps = {
                step.id: capability(session, self.tenant, step.capability) for step in plan.steps
            }
            ctx = self.context(session)
            if spec:
                pre = valid_preapproval(session, self.tenant, spec, parameters or {}, actor)
                if pre:
                    ctx = ctx.model_copy(
                        update={
                            "preapproval_id": pre.id,
                            "preapproval_steps": spec.approval.covers_steps,
                        }
                    )
            report = validate(plan, caps, identity, self.policy, ctx, trusted_form=spec is not None)
            # Store server-computed sources, discarding any labels supplied in Plan IR.
            clean = plan.model_copy(
                update={
                    "steps": tuple(
                        s.model_copy(update={"sources": report["steps"][i]["sources"]})
                        for i, s in enumerate(plan.steps)
                    )
                }
            )
            id_ = new_id("plan")
            put(
                session,
                Plan,
                self.tenant,
                id_,
                {
                    "plan": clean.model_dump(mode="json"),
                    "preview": report,
                    "requester_id": actor,
                    "recipe_name": spec.name if spec else None,
                    "recipe_version": spec.version if spec else None,
                    "parameters": parameters or {},
                },
                "preview",
            )
            append(
                session,
                self.tenant,
                id_,
                "plan.previewed",
                principal_id=actor,
                plan_id=id_,
                blocked=report["blocked"],
            )
            return {"id": id_, "plan": clean.model_dump(mode="json"), "preview": report}

    def confirm(self, actor: str, plan_id: str, *, _session: Session | None = None) -> RunEnvelope:
        with nullcontext(_session) if _session is not None else transaction(self.engine) as session:
            lock(session, f"confirm:{self.tenant}:{plan_id}")
            row = get(session, Plan, self.tenant, plan_id)
            if row.data["requester_id"] != actor:
                raise ValueError("only the requester can confirm this plan")
            plan = PlanIR.model_validate(row.data["plan"])
            caps = {s.id: capability(session, self.tenant, s.capability) for s in plan.steps}
            report = validate(
                plan,
                caps,
                self.identity(session, actor),
                self.policy,
                self.context(session),
                trusted_form=row.data["recipe_name"] is not None,
            )
            if report["blocked"]:
                raise ValueError("plan contains blocked steps; obtain the required role")
            if row.data.get("run_id"):
                run_id = row.data["run_id"]
            else:
                run_id = new_id("run")
                put(
                    session,
                    Run,
                    self.tenant,
                    run_id,
                    {
                        "plan_id": plan_id,
                        "on_behalf_of": actor,
                        "actor": actor,
                        "recipe_ref": f"{row.data['recipe_name']}@{row.data['recipe_version']}"
                        if row.data["recipe_name"]
                        else None,
                        "temporal_workflow_id": run_id,
                    },
                    "starting",
                )
                row.data = {**row.data, "run_id": run_id}
                row.status = "confirmed"
                append(
                    session,
                    self.tenant,
                    run_id,
                    "run.requested",
                    principal_id=actor,
                    run_id=run_id,
                    on_behalf_of=actor,
                    plan_id=plan_id,
                )
            envelope = RunEnvelope(
                tenant_id=self.tenant,
                run_id=run_id,
                requester_id=actor,
                plan=plan,
                caps=caps,
                sources={s.id: s.sources for s in plan.steps},
            )
            enqueue(
                session,
                self.tenant,
                run_id + ":start",
                run_id,
                "start",
                envelope.model_dump(mode="json"),
            )
            return envelope

    def run_recipe(
        self,
        actor: str,
        name: str,
        parameters: dict[str, Any],
        version: int | None = None,
        key: str | None = None,
    ) -> RunEnvelope:
        from copenhagen.core.canonical import sha256_hex

        if key is not None and not 1 <= len(key) <= 200:
            raise ValueError("Idempotency-Key must contain 1 to 200 characters")
        with transaction(self.engine) as session:
            request_id = "request:" + sha256_hex({"actor": actor, "key": key})
            content = sha256_hex({"name": name, "version": version, "parameters": parameters})
            if key:
                lock(session, f"request:{self.tenant}:{request_id}")
                existing = session.get(Nonce, (self.tenant, request_id))
                if existing:
                    if existing.data["content"] != content:
                        raise ValueError("Idempotency-Key already used for different parameters")
                    message = get(session, Outbox, self.tenant, existing.data["run_id"] + ":start")
                    return RunEnvelope.model_validate(message.data["value"])
            preview = self.preview(
                actor, recipe_name=name, parameters=parameters, version=version, _session=session
            )
            envelope = self.confirm(actor, preview["id"], _session=session)
            if key:
                put(
                    session,
                    Nonce,
                    self.tenant,
                    request_id,
                    {"content": content, "run_id": envelope.run_id},
                )
            return envelope

    def read_run(self, actor: str, id_: str) -> dict[str, Any]:
        with transaction(self.engine) as session:
            item = get(session, Run, self.tenant, id_)
            identity = self.identity(session, actor)
            approvals = [a for a in rows(session, Approval, self.tenant) if a.data["run_id"] == id_]
            readable = (
                actor == item.data["on_behalf_of"]
                or "auditor" in identity.roles
                or "admin" in identity.roles
                or any(set(a.data["required_roles"]) <= set(identity.roles) for a in approvals)
            )
            if not readable:
                raise ValueError("run is private to its requester and assigned approvers")
            return {
                "id": item.id,
                "status": item.status,
                **item.data,
                "steps": [
                    {"id": s.id, "status": s.status, **s.data}
                    for s in rows(session, StepRun, self.tenant)
                    if s.data["run_id"] == id_
                ],
            }

    def inbox(self, actor: str, *, tasks: bool = False) -> list[dict[str, Any]]:
        with transaction(self.engine) as session:
            identity = self.identity(session, actor)
            result = []
            for item in rows(session, HumanTask if tasks else Approval, self.tenant):
                required = {item.data["role"]} if tasks else set(item.data["required_roles"])
                if required <= set(identity.roles) and (
                    tasks or item.data["requester_id"] != actor
                ):
                    result.append({"id": item.id, "status": item.status, **item.data})
            return result

    def decide(
        self,
        actor: str,
        id_: str,
        approved: bool,
        reason: str,
        *,
        edits: dict[str, Any] | None = None,
        authenticated_at: datetime | None = None,
    ) -> dict[str, Any]:
        with transaction(self.engine) as session:
            lock(session, f"approval:{self.tenant}:{id_}")
            item = get(session, Approval, self.tenant, id_)
            identity = self.identity(session, actor)
            data = item.data
            if item.status != "pending":
                if data.get("decided_by") == actor and item.status == (
                    "approved" if approved else "rejected"
                ):
                    return {"id": id_, "step_id": data["step_id"], "run_id": data["run_id"]}
                raise ValueError("approval already decided")
            if data["requester_id"] == actor:
                raise ValueError("requesters cannot approve their own request (I6)")
            if approved and data.get("edited_by") == actor:
                raise ValueError("an independent approver must review your edit")
            if not set(data["required_roles"]) <= set(identity.roles):
                raise ValueError("required approver role is missing")
            if datetime.now(UTC) >= datetime.fromisoformat(data["expires_at"]):
                raise ValueError("approval has expired; requester must retry")
            if (
                data["risk_class"] in HIGH_RISK
                and not self.settings.copenhagen_dev_login
                and (
                    authenticated_at is None
                    or datetime.now(UTC) - authenticated_at > timedelta(minutes=15)
                )
            ):
                raise ValueError("fresh OIDC sign-in required for high-risk approval")
            values = {**data["inputs"], **(edits or {})}
            origin = {**data["sources"], **{k: "user" for k in edits or {}}}
            cap = capability(session, self.tenant, data["capability"])
            validate_values(cap.inputs, values)
            if approved and not self.policy.may_approve(
                identity, cap, values, self.context(session)
            ):
                raise ValueError("Cedar policy denies this approval")
            decision = self.policy.decide(
                self.identity(session, data["requester_id"]),
                cap,
                values,
                origin,
                self.context(session).model_copy(update={"runtime": True}),
            )
            if decision.outcome == "deny":
                raise ValueError("policy denies these resolved inputs; approval cannot override it")
            if not reason.strip():
                raise ValueError("approval decision requires a reason")
            changed = sorted(k for k, v in (edits or {}).items() if data["inputs"].get(k) != v)
            if approved and changed and data["risk_class"] in HIGH_RISK:
                # Changing a high-risk request makes it a new request. Whoever changed it
                # cannot also approve it, so it goes back to the queue for someone else.
                item.data = {
                    **data,
                    "inputs": values,
                    "sources": origin,
                    "inputs_hash": inputs_hash(values),
                    "edited_by": actor,
                    "edited_fields": changed,
                    "edit_reason": reason,
                }
                append(
                    session,
                    self.tenant,
                    f"{id_}:edited:{inputs_hash(values)}",
                    "approval.edited",
                    principal_id=actor,
                    run_id=data["run_id"],
                    step_id=data["step_id"],
                    inputs_hash=inputs_hash(values),
                    fields=changed,
                )
                enqueue(
                    session,
                    self.tenant,
                    f"notify:{id_}:edited:{inputs_hash(values)}",
                    None,
                    "notify",
                    notification(
                        "approval.requested",
                        tenant=self.tenant,
                        subject=f"Edited request needs independent review: {data['capability']}",
                        public_url=self.settings.public_url,
                        id=id_,
                        run_id=data["run_id"],
                        step_id=data["step_id"],
                        edited_by=actor,
                    ),
                )
                return {
                    "id": id_,
                    "step_id": data["step_id"],
                    "run_id": data["run_id"],
                    "status": "pending_review",
                }
            item.status = "approved" if approved else "rejected"
            item.data = {
                **data,
                "inputs": values,
                "sources": origin,
                "inputs_hash": inputs_hash(values),
                "decided_by": actor,
                "decided_at": datetime.now(UTC).isoformat(),
                "reason": reason,
            }
            append(
                session,
                self.tenant,
                f"{id_}:decided",
                "approval.decided",
                principal_id=actor,
                run_id=data["run_id"],
                step_id=data["step_id"],
                inputs_hash=inputs_hash(values),
                approver_id=actor,
                outcome=item.status,
            )
            result = {"id": id_, "step_id": data["step_id"], "run_id": data["run_id"]}
            enqueue(
                session, self.tenant, id_ + ":decided", data["run_id"], "approval_decided", result
            )
            return result

    def complete_task(self, actor: str, id_: str, outputs: dict[str, Any]) -> dict[str, str]:
        with transaction(self.engine) as session:
            lock(session, f"task:{self.tenant}:{id_}")
            item = get(session, HumanTask, self.tenant, id_)
            identity = self.identity(session, actor)
            if item.data["role"] not in identity.roles:
                raise ValueError("task assignee role required")
            if item.status != "pending":
                raise ValueError("task already completed")
            cap = capability(session, self.tenant, item.data["capability"])
            validate_values(cap.outputs, outputs)
            item.status = "completed"
            item.data = {**item.data, "outputs": outputs, "completed_by": actor}
            append(
                session,
                self.tenant,
                f"{id_}:completed",
                "task.completed",
                principal_id=actor,
                run_id=item.data["run_id"],
                step_id=item.data["step_id"],
            )
            result = {"id": id_, "run_id": item.data["run_id"], "step_id": item.data["step_id"]}
            enqueue(
                session,
                self.tenant,
                id_ + ":completed",
                item.data["run_id"],
                "task_completed",
                result,
            )
            return result

    def preapprove(self, actor: str, name: str, version: int) -> str:
        with transaction(self.engine) as session:
            return preapprove(
                session,
                self.tenant,
                recipe(session, self.tenant, name, version),
                self.identity(session, actor),
            )

    def action(
        self,
        actor: str,
        run_id: str,
        step: str | None,
        action: str,
        reason: str = "",
        *,
        compensate: bool = False,
    ) -> None:
        with transaction(self.engine) as session:
            if action not in {"retry", "skip", "cancel"}:
                raise ValueError("invalid recovery action")
            run = get(session, Run, self.tenant, run_id)
            identity = self.identity(session, actor)
            if actor != run.data["on_behalf_of"] and "admin" not in identity.roles:
                raise ValueError("only the requester or administrator can control this run")
            if run.data.get("terminal") or run.status in {
                "succeeded",
                "succeeded_with_skips",
                "cancelled",
                "compensated",
                "failed",
            }:
                raise ValueError("run is already terminal")
            if step:
                item = get(session, StepRun, self.tenant, f"{run_id}:{step}")
                if item.status != "needs_attention":
                    raise ValueError("step is not awaiting a human recovery decision")
            if action == "skip" and not reason.strip():
                raise ValueError("skip requires a reason")
            action_id = new_id("action")
            enqueue(
                session,
                self.tenant,
                action_id,
                run_id,
                "cancel" if action == "cancel" else "step_action",
                compensate
                if action == "cancel"
                else {"step_id": step, "action": action, "reason": reason, "id": action_id},
            )
            append(
                session,
                self.tenant,
                action_id,
                "run.human_action",
                principal_id=actor,
                run_id=run_id,
                step_id=step,
                action=action,
                reason=reason,
            )

    def assign_role(self, actor: str, target: str, role: str, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        if actor == target and role == "admin" and not enabled:
            raise ValueError("another administrator must revoke your administrator access")
        with transaction(self.engine) as session:
            lock(session, f"roles:{self.tenant}")
            if "admin" not in self.identity(session, actor).roles:
                raise ValueError("administrator role required")
            self.identity(session, target)
            get(session, Role, self.tenant, role)
            if actor == target and enabled and self._other_admins(session, actor):
                # With a second administrator available, nobody grants themselves power.
                raise ValueError("another administrator must grant you this role")
            put(
                session,
                PrincipalRole,
                self.tenant,
                f"{target}:{role}",
                {"principal_id": target, "role": role},
                "active" if enabled else "disabled",
            )
            append(
                session,
                self.tenant,
                new_id("role"),
                "role.changed",
                principal_id=actor,
                target=target,
                role=role,
                enabled=enabled,
            )

    def _other_admins(self, session: Session, actor: str) -> bool:
        for link in rows(session, PrincipalRole, self.tenant):
            if (
                link.status == "active"
                and link.data["role"] == "admin"
                and link.data["principal_id"] != actor
                and self.identity(session, link.data["principal_id"]).status == "active"
            ):
                return True
        return False

    def issue_key(
        self,
        actor: str,
        scopes: list[str],
        hours: int = 24,
        *,
        not_after: datetime | None = None,
    ) -> str:
        """Issue an API key. ``not_after`` caps expiry, so a key minted by another key
        can never outlive it and a leaked key cannot renew itself indefinitely."""

        allowed = {"read", "run", "approve", "admin"}
        if not scopes or not set(scopes) <= allowed or not 1 <= hours <= 720:
            raise ValueError("invalid key scopes or expiry")
        expires = datetime.now(UTC) + timedelta(hours=hours)
        if not_after is not None:
            expires = min(expires, not_after)
        with transaction(self.engine) as session:
            identity = self.identity(session, actor)
            if identity.status != "active":
                raise ValueError("principal disabled")
            if "admin" in scopes and "admin" not in identity.roles:
                raise ValueError("only admins can issue admin-scoped keys")
            raw = "cph_live_" + secrets.token_urlsafe(32)
            key_hash = hashlib.sha256(raw.encode()).hexdigest()
            id_ = new_id("key")
            put(
                session,
                ApiKey,
                self.tenant,
                key_hash,
                {
                    "principal_id": actor,
                    "scopes": scopes,
                    "expires_at": expires.isoformat(),
                    "key_id": id_,
                    "parent": "key" if not_after is not None else "session",
                },
            )
            append(session, self.tenant, id_, "api_key.issued", principal_id=actor, scopes=scopes)
            return raw
