"""Durable mock effects, shared by FakeAdapter and the HTTP mockworld service."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Literal, cast

from copenhagen.core.calls import CapabilityCall, Credential, InvokeResult, Preview
from copenhagen.core.canonical import sha256_hex
from copenhagen.core.capability import ExecutorSpec


class World:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS effects "
                "(key TEXT PRIMARY KEY, operation TEXT, inputs TEXT, outputs TEXT)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS faults (operation TEXT PRIMARY KEY, kind TEXT)")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def fault(self, operation: str, kind: str) -> None:
        if kind not in {"fail_next", "timeout", "rate_limit", "commit_then_drop", "needs_human"}:
            raise ValueError("unknown fault")
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO faults VALUES (?, ?)", (operation, kind))

    def invoke(self, operation: str, key: str, inputs: dict[str, Any]) -> InvokeResult:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            fault_row = db.execute(
                "SELECT kind FROM faults WHERE operation=?", (operation,)
            ).fetchone()
            if fault_row:
                db.execute("DELETE FROM faults WHERE operation=?", (operation,))
            fault = fault_row[0] if fault_row else None
            if fault in {"fail_next", "rate_limit", "needs_human", "timeout"}:
                return InvokeResult(
                    ok=False,
                    error_type=cast(
                        Literal["retryable", "not_retryable", "unknown_outcome", "needs_human"],
                        {
                            "fail_next": "not_retryable",
                            "rate_limit": "retryable",
                            "needs_human": "needs_human",
                            "timeout": "unknown_outcome",
                        }[fault],
                    ),
                    message=f"injected {fault}",
                    happened=False if fault != "timeout" else None,
                )
            if operation in {"verify", "get_refund", "get_effect"}:
                effect_key = inputs.get("idempotency_key")
                row = db.execute(
                    "SELECT outputs FROM effects WHERE key=?", (effect_key,)
                ).fetchone()
                if row is None:
                    return InvokeResult(
                        ok=False, error_type="not_retryable", message="not found", happened=False
                    )
                return InvokeResult(ok=True, outputs=json.loads(row[0]), happened=True)
            if operation == "get_order":
                orders = {
                    "1182": {
                        "payment_id": "pi_1182",
                        "total_cents": 30000,
                        "customer_email": "buyer@example.net",
                    },
                    "7500": {
                        "payment_id": "pi_7500",
                        "total_cents": 750000,
                        "customer_email": "buyer@example.net",
                    },
                }
                if str(inputs["order_id"]) not in orders:
                    return InvokeResult(
                        ok=False,
                        error_type="not_retryable",
                        message="order missing",
                        happened=False,
                    )
                return InvokeResult(ok=True, outputs=orders[str(inputs["order_id"])], happened=True)
            existing = db.execute(
                "SELECT inputs, outputs FROM effects WHERE key=?", (key,)
            ).fetchone()
            encoded = json.dumps(inputs, sort_keys=True)
            if existing:
                if existing[0] != encoded:
                    return InvokeResult(
                        ok=False,
                        error_type="not_retryable",
                        message="idempotency key input conflict",
                    )
                return InvokeResult(ok=True, outputs=json.loads(existing[1]), happened=True)
            short = sha256_hex({"key": key})[:12]
            if operation == "create_google_account":
                outputs = {
                    "work_email": inputs["full_name"].lower().replace(" ", ".") + "@example.com",
                    "status": "active",
                }
            elif operation == "refund":
                outputs = {"refund_id": "re_" + short, "status": "succeeded"}
            elif operation == "customer_create":
                outputs = {"customer_id": "cus_" + short, "status": "active"}
            elif operation == "trading_release":
                outputs = {"release_id": "release_" + short, "status": "paper_active"}
            else:
                outputs = {"record_id": "rec_" + short, "status": "succeeded"}
            db.execute(
                "INSERT INTO effects VALUES (?, ?, ?, ?)",
                (key, operation, encoded, json.dumps(outputs)),
            )
            if fault == "commit_then_drop":
                return InvokeResult(
                    ok=False,
                    error_type="unknown_outcome",
                    message="committed then connection dropped",
                )
            return InvokeResult(ok=True, outputs=outputs, happened=True)

    def effects(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                {
                    "key": key,
                    "operation": op,
                    "inputs": json.loads(ins),
                    "outputs": json.loads(outs),
                }
                for key, op, ins, outs in db.execute("SELECT * FROM effects ORDER BY rowid")
            ]


class FakeAdapter:
    kind = "fake"

    def __init__(self, world: World) -> None:
        self.world = world

    async def validate_config(self, executor: ExecutorSpec) -> None:
        if executor.adapter != "fake":
            raise ValueError("incorrect adapter")

    async def preview(self, call: CapabilityCall, cred: Credential) -> Preview:
        return Preview(summary="Mockworld dry run")

    async def invoke(self, call: CapabilityCall, cred: Credential) -> InvokeResult:
        return self.world.invoke(
            call.capability.executor.operation, call.idempotency_key, call.inputs
        )

    async def poll(self, handle: str, cred: Credential) -> InvokeResult:
        return InvokeResult(ok=False, error_type="needs_human", message="manual result needed")

    async def cancel(self, handle: str, cred: Credential) -> None:
        raise ValueError("use an explicit compensation capability")
