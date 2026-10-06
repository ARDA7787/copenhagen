"""Tell people when work is waiting for them.

Notifications travel through the transactional outbox, so an approval request is never
committed without its notification eventually being sent. They are queued without a run
ordering key: a broken notification channel can never hold back a run's own messages.

The payload is deliberately small: what is waiting, for whom, and a link. Step inputs are
never included, because they may hold customer data or secrets; approvers open the link
and sign in to see them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

KINDS = {"approval.requested", "task.created", "run.needs_attention", "run.orphaned"}


class NotificationError(Exception):
    """The channel failed; the outbox retries with backoff, then dead-letters."""


class Notifier(Protocol):
    async def send(self, event: dict[str, Any]) -> None: ...


class LogNotifier:
    """Default channel: a structured log line an operator's log pipeline can route."""

    async def send(self, event: dict[str, Any]) -> None:
        logger.info("Notification", extra={"notification": event})


def signature(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


class WebhookNotifier:
    """POSTs each notification as signed JSON to one company-owned endpoint.

    Receivers verify ``X-Copenhagen-Signature`` over ``"{timestamp}.{body}"`` and reject
    timestamps older than five minutes.
    """

    def __init__(
        self,
        url: str,
        secret: str,
        *,
        allow_insecure: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https" and not (allow_insecure and parts.scheme == "http"):
            raise ValueError("notification webhook must use HTTPS")
        if not parts.hostname or parts.username or parts.password:
            raise ValueError("notification webhook URL is invalid")
        if len(secret) < 32:
            raise ValueError("notification webhook secret must be at least 32 characters")
        self.url, self.secret = url, secret
        self.client = client or httpx.AsyncClient(
            timeout=10, follow_redirects=False, trust_env=False
        )

    async def send(self, event: dict[str, Any]) -> None:
        body = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        try:
            response = await self.client.post(
                self.url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Copenhagen-Timestamp": timestamp,
                    "X-Copenhagen-Signature": signature(self.secret, timestamp, body),
                    "X-Copenhagen-Event": event["kind"],
                },
            )
        except httpx.HTTPError as error:
            raise NotificationError(type(error).__name__) from error
        if response.status_code >= 300:
            raise NotificationError(f"webhook returned HTTP {response.status_code}")


def notification(
    kind: str, *, tenant: str, subject: str, public_url: str, **fields: Any
) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"unknown notification kind {kind}")
    link = {
        "approval.requested": "/approvals",
        "task.created": "/tasks",
    }.get(kind, f"/runs/{fields.get('run_id', '')}")
    return {
        "kind": kind,
        "tenant_id": tenant,
        "subject": subject,
        "link": public_url.rstrip("/") + link,
        **{k: v for k, v in fields.items() if v is not None},
    }


def build(url: str | None, secret: str | None, *, allow_insecure: bool = False) -> Notifier:
    if url:
        if not secret:
            raise ValueError("notification webhook requires a secret")
        return WebhookNotifier(url, secret, allow_insecure=allow_insecure)
    return LogNotifier()
