"""Opaque server-side sessions, CSRF, scoped hashed API keys and OIDC state storage."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from copenhagen.db.models import ApiKey, Session
from copenhagen.db.store import put, transaction
from copenhagen.service import Service

COOKIE = "cph_session"


class SessionMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, service: Service) -> None:
        super().__init__(app)
        self.service = service

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        service = self.service
        raw = request.cookies.get(COOKIE, "")
        hashed = hashlib.sha256(raw.encode()).hexdigest() if raw else ""
        request.state.actor = None
        request.state.scopes = set()
        request.state.authenticated_at = None
        session_data: dict[str, Any] = {}
        with transaction(service.engine) as db:
            record = db.get(Session, (service.tenant, hashed)) if hashed else None
            if (
                record
                and record.status == "active"
                and datetime.now(UTC) < datetime.fromisoformat(record.data["expires_at"])
            ):
                session_data = dict(record.data)
                request.state.actor = session_data.get("principal_id")
                request.state.scopes = {"read", "run", "approve", "admin"}
                if session_data.get("authenticated_at"):
                    request.state.authenticated_at = datetime.fromisoformat(
                        session_data["authenticated_at"]
                    )
            bearer = request.headers.get("authorization", "")
            if bearer.startswith("Bearer "):
                api_hash = hashlib.sha256(bearer[7:].encode()).hexdigest()
                key = db.get(ApiKey, (service.tenant, api_hash))
                if (
                    not key
                    or key.status != "active"
                    or datetime.now(UTC) >= datetime.fromisoformat(key.data["expires_at"])
                ):
                    return Response("invalid or expired API key", status_code=401)
                request.state.actor = key.data["principal_id"]
                request.state.scopes = set(key.data["scopes"])
                request.state.api_key = True
                request.state.authenticated_at = None
            if request.state.actor:
                try:
                    identity = service.identity(db, request.state.actor)
                    if identity.status != "active":
                        return Response("principal disabled", status_code=401)
                except ValueError:
                    return Response("unknown principal", status_code=401)
        if not session_data and request.url.path == "/auth/login":
            raw = secrets.token_urlsafe(32)
            hashed = hashlib.sha256(raw.encode()).hexdigest()
            session_data = {
                "principal_id": None,
                "csrf": secrets.token_urlsafe(32),
                "oauth": {},
                "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            }
            with transaction(service.engine) as db:
                put(db, Session, service.tenant, hashed, session_data)
            request.state.new_cookie = raw
        request.scope["session"] = dict(session_data.get("oauth", {}))
        request.state.csrf = session_data.get("csrf", "")
        request.state.session_hash = hashed
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not getattr(
            request.state, "api_key", False
        ):
            exempt = (
                request.url.path.startswith(("/v1/hooks/", "/v1/callbacks/"))
                or request.url.path == "/auth/dev"
            )
            if not exempt:
                token = request.headers.get("x-csrf-token", "")
                if not token and request.headers.get("content-type", "").startswith(
                    "application/x-www-form-urlencoded"
                ):
                    await request.body()
                    form = await request.form()
                    token = str(form.get("csrf", ""))
                if (
                    not session_data
                    or not token
                    or not secrets.compare_digest(token, request.state.csrf)
                ):
                    return Response("CSRF token required", status_code=403)
            elif request.url.path == "/auth/dev":
                origin = request.headers.get("origin")
                if origin and origin != service.settings.public_url:
                    return Response("invalid login origin", status_code=403)
        response = await call_next(request)
        if session_data and hashed and not getattr(request.state, "rotated_session", False):
            with transaction(service.engine) as db:
                row = db.get(Session, (service.tenant, hashed))
                if row and row.status == "active":
                    row.data = {**row.data, "oauth": request.scope["session"]}
        if getattr(request.state, "new_cookie", None):
            response.set_cookie(
                COOKIE,
                request.state.new_cookie,
                httponly=True,
                secure=service.settings.env == "prod",
                samesite="lax",
                max_age=service.settings.session_hours * 3600,
            )
        return response


def login(service: Service, request: Request, actor: str) -> None:
    raw = secrets.token_urlsafe(32)
    with transaction(service.engine) as db:
        identity = service.identity(db, actor)
        if identity.status != "active" or identity.kind != "user":
            raise ValueError("user login is unavailable")
        previous = db.get(Session, (service.tenant, request.state.session_hash))
        if previous:
            previous.status = "revoked"
        now = datetime.now(UTC)
        put(
            db,
            Session,
            service.tenant,
            hashlib.sha256(raw.encode()).hexdigest(),
            {
                "principal_id": actor,
                "csrf": secrets.token_urlsafe(32),
                "oauth": {},
                "authenticated_at": now.isoformat(),
                "expires_at": (now + timedelta(hours=service.settings.session_hours)).isoformat(),
            },
        )
        from copenhagen.audit.chain import append
        from copenhagen.db.store import new_id

        append(db, service.tenant, new_id("login"), "session.started", principal_id=actor)
    request.state.rotated_session = True
    request.state.new_cookie = raw


def actor(request: Request, scope: str = "read") -> str:
    if not request.state.actor:
        raise HTTPException(401, "sign in required")
    if scope not in request.state.scopes:
        raise HTTPException(403, f"API key requires {scope} scope")
    return request.state.actor
