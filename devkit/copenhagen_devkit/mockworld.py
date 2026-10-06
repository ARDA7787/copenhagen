"""Development-only existing-business API simulator with a persistent world view."""

import html
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from copenhagen_devkit.fake import World


def create_app(path: str = ".data/mockworld.sqlite") -> FastAPI:
    if os.environ.get("ENV", "dev") == "prod":
        raise ValueError("mockworld is a development service")
    world = World(path)
    app = FastAPI(title="Copenhagen mockworld")

    @app.get("/", response_class=HTMLResponse)
    def view() -> str:
        import json

        return (
            "<h1>Mock business effects</h1><pre>"
            + html.escape(json.dumps(world.effects(), indent=2))
            + "</pre>"
        )

    @app.get("/effects")
    def effects() -> list[dict[str, Any]]:
        return world.effects()

    @app.post("/faults/{operation}/{kind}")
    def faults(operation: str, kind: str) -> dict[str, bool]:
        world.fault(operation, kind)
        return {"injected": True}

    @app.get("/operations/{operation}")
    def read(operation: str, order_id: str | None = None, idempotency_key: str | None = None):
        values = {
            k: v
            for k, v in {"order_id": order_id, "idempotency_key": idempotency_key}.items()
            if v is not None
        }
        return response(world.invoke(operation, "read", values))

    @app.post("/operations/{operation}")
    def write(operation: str, inputs: dict[str, Any], idempotency_key: str = Header()):
        return response(world.invoke(operation, idempotency_key, inputs))

    def response(result):
        if result.ok:
            return result.outputs
        if result.message == "committed then connection dropped":
            return JSONResponse({"message": result.message}, status_code=504)
        code = {"retryable": 429, "not_retryable": 404, "needs_human": 409, "unknown_outcome": 504}[
            result.error_type
        ]
        raise HTTPException(code, result.message)

    return app
