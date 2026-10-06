"""All database access and audit writes occur on the control task queue."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session
from temporalio import activity
from temporalio.exceptions import ApplicationError

from copenhagen.audit.chain import append
from copenhagen.core.calls import CapabilityCall
from copenhagen.core.canonical import idempotency_key, inputs_hash
from copenhagen.core.capability import duration_seconds
from copenhagen.core.identity import Decision, RunContext
from copenhagen.core.plan import PlanIR
from copenhagen.core.schema import validate_values
from copenhagen.db.models import Approval, HumanTask, Plan, Run, StepRun, Tenant
from copenhagen.db.store import get, put, rows, transaction
from copenhagen.engine.contracts import ControlArgs, ControlResult
from copenhagen.engine.dispatch import enqueue
from copenhagen.notify import notification
from copenhagen.policy.budgets import reserve, settle
from copenhagen.policy.engine import PolicyEngine
from copenhagen.policy.identity import principal
from copenhagen.policy.preapproval import valid_preapproval
from copenhagen.registry.publish import capability, recipe
from copenhagen.secrets.broker import authorization
from copenhagen.settings import Settings


class ControlActivities:
    def __init__(
        self, engine: Engine, settings: Settings, policy: PolicyEngine | None = None
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.policy = policy or PolicyEngine(directory=settings.policy_directory)
        self.signing_key = os.environ.get("COPENHAGEN_AUTHORIZATION_KEY", "dev-control-key")
        if settings.env == "prod" and self.signing_key == "dev-control-key":
            raise ValueError("production requires a control authorization key")

    def notify(
        self, session: Session, args: ControlArgs, kind: str, id_: str, **fields: Any
    ) -> None:
        subject = fields.pop("subject")
        enqueue(
            session,
            args.tenant_id,
            f"notify:{id_}",
            None,
            "notify",
            notification(
                kind,
                tenant=args.tenant_id,
                subject=subject,
                public_url=self.settings.public_url,
                id=id_,
                run_id=args.run_id,
                step_id=args.step_id or None,
                **fields,
            ),
        )

    @activity.defn(name="control")
    async def execute(self, args: ControlArgs) -> ControlResult:
        try:
            return await asyncio.to_thread(self.perform, args)
        except ValueError as error:
            # Invalid resolved inputs must not leave a workflow retrying the control
            # activity forever. A denial is recorded by the interpreter.
            if args.action == "policy":
                return ControlResult(
                    decision=Decision(
                        outcome="deny",
                        reasons=("resolved inputs or capability configuration invalid",),
                        policy_version=self.policy.policy_version,
                    )
                )
            raise ApplicationError(str(error), non_retryable=True) from error

    def perform(self, args: ControlArgs) -> ControlResult:
        with transaction(self.engine) as session:
            run = get(session, Run, args.tenant_id, args.run_id)
            plan_row = get(session, Plan, args.tenant_id, run.data["plan_id"])
            plan = PlanIR.model_validate(plan_row.data["plan"])
            requester = principal(session, args.tenant_id, run.data["on_behalf_of"])
            tenant = session.get(Tenant, args.tenant_id)
            assert tenant is not None
            config = tenant.config
            step = next((s for s in plan.steps if s.id == args.step_id), None)
            # Compensation/verification are registered subcalls, never arbitrary capabilities.
            cap_ref = step.capability if step else args.data.get("capability")
            if args.data.get("subcall"):
                original = capability(
                    session,
                    args.tenant_id,
                    step.capability if step else args.data["original_capability"],
                )
                permitted: set[str] = (
                    {original.verify.capability} if original.verify != "none" else set()
                )
                if original.compensate != "none":
                    permitted.add(
                        original.compensate
                        if isinstance(original.compensate, str)
                        else original.compensate.capability
                    )
                if args.data["capability"] not in permitted:
                    raise ValueError("unregistered subcall")
                cap_ref = args.data["capability"]
            cap = capability(session, args.tenant_id, cap_ref) if cap_ref else None
            if args.action == "policy":
                assert cap is not None
                validate_values(cap.inputs, args.inputs)
                preapproval = None
                covered: tuple[str, ...] = ()
                if plan_row.data.get("recipe_name") and not args.data.get("subcall"):
                    spec = recipe(
                        session,
                        args.tenant_id,
                        plan_row.data["recipe_name"],
                        plan_row.data["recipe_version"],
                    )
                    preapproval = valid_preapproval(
                        session, args.tenant_id, spec, plan_row.data["parameters"], requester.id
                    )
                    covered = spec.approval.covers_steps
                ctx = RunContext(
                    env=self.settings.env,
                    runtime=True,
                    step_id=args.step_id,
                    internal_domains=frozenset(config.get("internal_domains", [])),
                    kill_switch=config.get("kill_switch", False),
                    money_ceiling=config.get("money_ceiling", 500000),
                    preapproval_id=preapproval.id if preapproval else None,
                    preapproval_steps=covered,
                )
                decision = self.policy.decide(requester, cap, args.inputs, args.sources, ctx)
                h = inputs_hash(args.inputs, args.data.get("preview_artifact_hash"))
                approval_id = args.data.get("approval_id")
                if approval_id and decision.outcome == "needs_approval":
                    record = get(session, Approval, args.tenant_id, approval_id)
                    approver = principal(
                        session, args.tenant_id, record.data.get("decided_by", requester.id)
                    )
                    expires = datetime.fromisoformat(record.data["expires_at"])
                    valid = (
                        record.status == "approved"
                        and record.data["run_id"] == args.run_id
                        and record.data["step_id"] == args.step_id
                        and record.data["inputs_hash"] == h
                        and record.data["requester_id"] != approver.id
                        and approver.status == "active"
                        and set(decision.approver_roles) <= set(approver.roles)
                        and datetime.now(UTC) < expires
                    )
                    if valid:
                        decision = decision.model_copy(
                            update={
                                "outcome": "allow",
                                "approved_via": f"approval:{record.id}",
                                "reasons": ("independent approval verified",),
                            }
                        )
                key = f"{args.run_id}:{args.step_id}:{h}"
                if decision.outcome == "allow" and cap.risk.class_ != "read":
                    cost = {
                        "money_cents": args.inputs.get("amount_cents", 0)
                        if cap.risk.class_ == "financial"
                        else 0,
                        "messages": int(cap.executor.queue == "comms"),
                        "records": 1,
                    }
                    limits = {
                        "money_cents": config.get("daily_money_cents", 1000000),
                        "messages": config.get("daily_messages", 1000),
                        "records": config.get("daily_records", 10000),
                    }
                    scopes = (
                        "tenant",
                        f"principal:{requester.id}",
                        f"capability:{cap.ref}",
                        f"run:{args.run_id}",
                    )
                    for scope in scopes:
                        scope_limits = (
                            {**limits, "money_cents": ctx.money_ceiling}
                            if scope.startswith("run:")
                            else limits
                        )
                        if not reserve(session, args.tenant_id, scope, key, cost, scope_limits):
                            for release_scope in scopes:
                                settle(session, args.tenant_id, release_scope, key, commit=False)
                            decision = decision.model_copy(
                                update={
                                    "outcome": "deny",
                                    "reasons": ("daily usage budget exceeded",),
                                }
                            )
                            break
                event_key = (
                    f"{key}:policy:{args.attempt}:{decision.outcome}:"
                    f"{decision.approved_via or 'none'}"
                )
                append(
                    session,
                    args.tenant_id,
                    event_key,
                    "policy.decided",
                    run_id=args.run_id,
                    step_id=args.step_id,
                    inputs_hash=h,
                    capability=cap.ref,
                    principal_id=requester.id,
                    policy_version=decision.policy_version,
                    decision=decision.outcome,
                    approved_via=decision.approved_via,
                    sources=args.sources,
                )
                token = ""
                if decision.outcome == "allow":
                    call = CapabilityCall(
                        tenant_id=args.tenant_id,
                        run_id=args.run_id,
                        step_id=args.step_id,
                        capability=cap,
                        inputs=args.inputs,
                        inputs_hash=h,
                        idempotency_key=idempotency_key(args.run_id, args.step_id, h),
                        preview_artifact_hash=args.data.get("preview_artifact_hash"),
                    )
                    token = authorization(self.signing_key, call)
                return ControlResult(
                    decision=decision,
                    authorization=token,
                    data={"capability": cap.model_dump(mode="json", by_alias=True)},
                )
            if args.action == "approval":
                assert cap is not None
                h = inputs_hash(args.inputs, args.data.get("preview_artifact_hash"))
                id_ = f"{args.run_id}:{args.step_id}:{h}:{args.attempt}"
                if session.get(Approval, (args.tenant_id, id_)) is None:
                    expires = datetime.now(UTC) + timedelta(
                        seconds=duration_seconds(cap.approval.expires_after)
                    )
                    put(
                        session,
                        Approval,
                        args.tenant_id,
                        id_,
                        {
                            "run_id": args.run_id,
                            "step_id": args.step_id,
                            "capability": cap.ref,
                            "inputs_hash": h,
                            "inputs": args.inputs,
                            "sources": args.sources,
                            "requester_id": requester.id,
                            "required_roles": args.data["roles"],
                            "expires_at": expires.isoformat(),
                            "risk_class": cap.risk.class_,
                        },
                        "pending",
                    )
                    append(
                        session,
                        args.tenant_id,
                        id_,
                        "approval.requested",
                        run_id=args.run_id,
                        step_id=args.step_id,
                        inputs_hash=h,
                        principal_id=requester.id,
                    )
                    self.notify(
                        session,
                        args,
                        "approval.requested",
                        id_,
                        subject=f"Approval needed: {cap.ref}",
                        capability=cap.ref,
                        role=cap.approval.approver_role,
                        requester=requester.id,
                    )
                run.status = "waiting_approval"
                return ControlResult(id=id_)
            if args.action == "approval_result":
                record = get(session, Approval, args.tenant_id, args.data["id"])
                return ControlResult(id=record.id, data={**record.data, "status": record.status})
            if args.action == "task":
                assert cap is not None
                id_ = f"{args.run_id}:{args.step_id}:task:{args.attempt}"
                if session.get(HumanTask, (args.tenant_id, id_)) is None:
                    put(
                        session,
                        HumanTask,
                        args.tenant_id,
                        id_,
                        {
                            "run_id": args.run_id,
                            "step_id": args.step_id,
                            "capability": cap.ref,
                            "role": cap.approval.approver_role or f"approver:{cap.executor.queue}",
                            "instructions": cap.description,
                            "inputs": args.inputs,
                        },
                        "pending",
                    )
                    append(
                        session,
                        args.tenant_id,
                        id_,
                        "task.created",
                        run_id=args.run_id,
                        step_id=args.step_id,
                    )
                    self.notify(
                        session,
                        args,
                        "task.created",
                        id_,
                        subject=f"Task assigned: {cap.ref}",
                        capability=cap.ref,
                        role=cap.approval.approver_role or f"approver:{cap.executor.queue}",
                    )
                run.status = "needs_attention"
                return ControlResult(id=id_)
            if args.action == "task_result":
                record = get(session, HumanTask, args.tenant_id, args.data["id"])
                return ControlResult(data={**record.data, "status": record.status})
            if args.action == "record":
                event = args.data["event"]
                h = args.data.get("inputs_hash")
                if event == "step.needs_attention":
                    self.notify(
                        session,
                        args,
                        "run.needs_attention",
                        args.data["event_id"],
                        subject=f"Run needs attention at step {args.step_id}",
                        reason=args.data.get("reason"),
                        requester=requester.id,
                    )
                if event == "approval.expired":
                    for approval in rows(session, Approval, args.tenant_id):
                        if (
                            approval.status == "pending"
                            and approval.data["run_id"] == args.run_id
                            and approval.data["step_id"] == args.step_id
                        ):
                            approval.status = "expired"
                append(
                    session,
                    args.tenant_id,
                    args.data["event_id"],
                    event,
                    run_id=args.run_id,
                    step_id=args.step_id,
                    attempt=args.attempt,
                    **{
                        k: v
                        for k, v in args.data.items()
                        if k
                        not in {
                            "event",
                            "event_id",
                            "status",
                            "outputs",
                            "subcall",
                            "original_capability",
                        }
                    },
                )
                if args.step_id:
                    old = session.get(StepRun, (args.tenant_id, f"{args.run_id}:{args.step_id}"))
                    data = {
                        **(old.data if old else {}),
                        "run_id": args.run_id,
                        "step_id": args.step_id,
                        "attempt": args.attempt,
                        **{k: v for k, v in args.data.items() if k not in {"event", "event_id"}},
                    }
                    put(
                        session,
                        StepRun,
                        args.tenant_id,
                        f"{args.run_id}:{args.step_id}",
                        data,
                        args.data.get("status", old.status if old else "running"),
                    )
                if h and cap and args.data.get("status") in {"succeeded", "failed", "skipped"}:
                    for scope in (
                        "tenant",
                        f"principal:{requester.id}",
                        f"capability:{cap.ref}",
                        f"run:{args.run_id}",
                    ):
                        settle(
                            session,
                            args.tenant_id,
                            scope,
                            f"{args.run_id}:{args.step_id}:{h}",
                            commit=args.data["status"] == "succeeded",
                        )
                if args.data.get("run_status"):
                    run.status = args.data["run_status"]
                    run.data = {**run.data, "result": args.data.get("result", {})}
                elif event == "step.started":
                    run.status = "running"
                if event == "run.finished":
                    run.data = {
                        **run.data,
                        "terminal": True,
                        "ended_at": datetime.now(UTC).isoformat(),
                    }
                    for model in (Approval, HumanTask):
                        for item in rows(session, model, args.tenant_id):
                            if item.status == "pending" and item.data["run_id"] == args.run_id:
                                item.status = "cancelled"
                return ControlResult()
            raise ValueError(f"unknown control action {args.action}")
