"""Durable, sanitized operations notification delivery."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import ssl
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..utils import utc_now
from .contracts import StateError
from .store import StateStore


SEVERITY_ORDER = {
    "LOW": 0,
    "MEDIUM": 1,
    "HIGH": 2,
    "CRITICAL": 3,
}


@dataclass(frozen=True)
class NotificationSettings:
    enabled: bool
    webhook_url: str | None
    signing_key: bytes | None
    min_severity: str
    max_attempts: int
    request_timeout_seconds: float
    initial_backoff_seconds: float
    max_backoff_seconds: float
    dispatch_interval_seconds: float
    required_delivery_timeout_seconds: float
    maintenance_delivery_required: bool


def notification_settings(
    project: dict[str, Any],
    *,
    environ: dict[str, str] | None = None,
) -> NotificationSettings:
    raw_operations = project.get("operations") or {}
    if not isinstance(raw_operations, dict):
        raise StateError(
            "operations configuration must be an object",
            error_code="OPS_NOTIFICATION_CONFIG_INVALID",
        )
    raw = raw_operations.get("notifications") or {}
    if not isinstance(raw, dict):
        raise StateError(
            "operations.notifications must be an object",
            error_code="OPS_NOTIFICATION_CONFIG_INVALID",
        )
    enabled = raw.get("enabled", False)
    maintenance_required = raw.get(
        "maintenance_delivery_required",
        True,
    )
    if not isinstance(enabled, bool) or not isinstance(
        maintenance_required,
        bool,
    ):
        raise StateError(
            "notification flags must be booleans",
            error_code="OPS_NOTIFICATION_CONFIG_INVALID",
        )
    min_severity = str(raw.get("min_severity", "HIGH"))
    if min_severity not in SEVERITY_ORDER:
        raise StateError(
            "notification minimum severity is invalid",
            error_code="OPS_NOTIFICATION_CONFIG_INVALID",
        )
    env = os.environ if environ is None else environ
    url_env = str(
        raw.get("webhook_url_env", "EVO_WIKI_OPS_WEBHOOK_URL")
    )
    key_env = str(
        raw.get("signing_key_env", "EVO_WIKI_OPS_WEBHOOK_KEY")
    )
    webhook_url = env.get(url_env)
    raw_key = env.get(key_env)
    signing_key = raw_key.encode("utf-8") if raw_key else None
    settings = NotificationSettings(
        enabled=enabled,
        webhook_url=webhook_url,
        signing_key=signing_key,
        min_severity=min_severity,
        max_attempts=_bounded_int(
            raw.get("max_attempts", 3),
            minimum=1,
            maximum=20,
        ),
        request_timeout_seconds=_bounded_float(
            raw.get("request_timeout_seconds", 5),
            minimum=0.1,
            maximum=60,
        ),
        initial_backoff_seconds=_bounded_float(
            raw.get("initial_backoff_seconds", 1),
            minimum=0,
            maximum=300,
        ),
        max_backoff_seconds=_bounded_float(
            raw.get("max_backoff_seconds", 30),
            minimum=0,
            maximum=3600,
        ),
        dispatch_interval_seconds=_bounded_float(
            raw.get("dispatch_interval_seconds", 2),
            minimum=0.1,
            maximum=300,
        ),
        required_delivery_timeout_seconds=_bounded_float(
            raw.get("required_delivery_timeout_seconds", 15),
            minimum=1,
            maximum=300,
        ),
        maintenance_delivery_required=maintenance_required,
    )
    if (
        settings.initial_backoff_seconds
        > settings.max_backoff_seconds
    ):
        raise StateError(
            "notification backoff range is invalid",
            error_code="OPS_NOTIFICATION_CONFIG_INVALID",
        )
    if enabled:
        if not webhook_url:
            raise StateError(
                "notification webhook URL is missing",
                error_code="OPS_NOTIFICATION_WEBHOOK_MISSING",
            )
        _validate_webhook_url(webhook_url)
        if signing_key is None or len(signing_key) < 32:
            raise StateError(
                "notification signing key is missing or too short",
                error_code="OPS_NOTIFICATION_SIGNING_KEY_MISSING",
            )
    return settings


def should_notify(
    settings: NotificationSettings,
    severity: str,
) -> bool:
    return (
        settings.enabled
        and severity in SEVERITY_ORDER
        and SEVERITY_ORDER[severity]
        >= SEVERITY_ORDER[settings.min_severity]
    )


def build_notification(
    *,
    root: Path,
    event_type: str,
    severity: str,
    subject_type: str,
    subject_id: str,
    dedupe_key: str,
    security_domain: str,
    state: str | None = None,
    error_code: str | None = None,
    counts: dict[str, int] | None = None,
    delivery_required: bool = False,
    max_attempts: int = 3,
) -> dict[str, Any]:
    event_id = _deterministic_event_id(dedupe_key)
    payload = {
        "schema_version": 1,
        "event_id": event_id,
        "event_type": event_type,
        "severity": severity,
        "occurred_at": utc_now(),
        "source": "evo-wiki",
        "workspace_fingerprint": "sha256:"
        + hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest(),
        "security_domain": security_domain,
        "subject": {
            "type": subject_type,
            "id": subject_id,
        },
        "state": state,
        "error_code": error_code,
        "counts": counts or {},
    }
    return {
        "dedupe_key": dedupe_key,
        "event_type": event_type,
        "severity": severity,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "payload": payload,
        "delivery_required": delivery_required,
        "max_attempts": max_attempts,
    }


class NotificationDispatcher:
    """Claims durable events, performs HTTP outside SQLite, records outcomes."""

    def __init__(
        self,
        store: StateStore,
        settings: NotificationSettings,
        *,
        worker_id: str | None = None,
    ):
        if not settings.enabled:
            raise StateError(
                "operations notifications are disabled",
                error_code="OPS_NOTIFICATION_DISABLED",
            )
        self.store = store
        self.settings = settings
        self.worker_id = worker_id or f"notify-{uuid.uuid4().hex}"

    def dispatch_due(self, *, limit: int = 20) -> dict[str, Any]:
        claimed = self.store.claim_due_notifications(
            worker_id=self.worker_id,
            limit=limit,
        )
        delivered = 0
        retry_wait = 0
        failed = 0
        results: list[dict[str, Any]] = []
        for item in claimed:
            outcome, status_class, error_code = self._deliver(item)
            attempt = int(item["attempt_count"])
            backoff = min(
                self.settings.max_backoff_seconds,
                self.settings.initial_backoff_seconds
                * (2 ** max(0, attempt - 1)),
            )
            terminal = self.store.finish_notification_attempt(
                notification_id=str(item["id"]),
                worker_id=self.worker_id,
                outcome=outcome,
                http_status_class=status_class,
                error_code=error_code,
                retry_after_seconds=backoff,
            )
            status = str(terminal["status"])
            delivered += int(status == "DELIVERED")
            retry_wait += int(status == "RETRY_WAIT")
            failed += int(status == "FAILED")
            results.append(
                {
                    "notification_id": str(item["id"]),
                    "status": status,
                    "attempt_count": int(terminal["attempt_count"]),
                    "error_code": terminal.get("last_error_code"),
                }
            )
        return {
            "schema_version": 1,
            "status": (
                "failed"
                if failed
                else "retry_wait"
                if retry_wait
                else "delivered"
                if delivered
                else "no_pending"
            ),
            "claimed": len(claimed),
            "delivered": delivered,
            "retry_wait": retry_wait,
            "failed": failed,
            "items": results,
            "workspace_mutated": bool(claimed),
            "error_code": (
                "OPS_NOTIFICATION_DELIVERY_FAILED" if failed else None
            ),
        }

    def _deliver(
        self,
        item: dict[str, Any],
    ) -> tuple[str, str | None, str | None]:
        assert self.settings.webhook_url is not None
        assert self.settings.signing_key is not None
        payload = item["payload"]
        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        timestamp = utc_now()
        signature = hmac.new(
            self.settings.signing_key,
            timestamp.encode("utf-8") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        parsed = urlsplit(self.settings.webhook_url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        connection: http.client.HTTPConnection
        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(
                parsed.hostname,
                parsed.port or 443,
                timeout=self.settings.request_timeout_seconds,
                context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(
                parsed.hostname,
                parsed.port or 80,
                timeout=self.settings.request_timeout_seconds,
            )
        try:
            connection.request(
                "POST",
                path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "X-Evo-Event-ID": str(item["id"]),
                    "X-Evo-Timestamp": timestamp,
                    "X-Evo-Signature": f"v1={signature}",
                },
            )
            response = connection.getresponse()
            status = int(response.status)
            response.read(1024)
        except (OSError, TimeoutError, http.client.HTTPException):
            return (
                "RETRYABLE",
                None,
                "OPS_NOTIFICATION_REQUEST_FAILED",
            )
        finally:
            connection.close()
        status_class = f"{status // 100}XX"
        if 200 <= status < 300:
            return "DELIVERED", status_class, None
        if status in {408, 429} or 500 <= status < 600:
            return (
                "RETRYABLE",
                status_class,
                "OPS_NOTIFICATION_REMOTE_RETRYABLE",
            )
        return (
            "TERMINAL",
            status_class,
            "OPS_NOTIFICATION_REMOTE_REJECTED",
        )


def _validate_webhook_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise StateError(
            "notification webhook URL is invalid",
            error_code="OPS_NOTIFICATION_WEBHOOK_INVALID",
        )
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        raise StateError(
            "non-loopback notification webhook requires HTTPS",
            error_code="OPS_NOTIFICATION_WEBHOOK_INSECURE",
        )


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _deterministic_event_id(dedupe_key: str) -> str:
    return "notification-" + hashlib.sha256(
        dedupe_key.encode("utf-8")
    ).hexdigest()[:32]


def _bounded_int(value: Any, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise StateError(
            "notification numeric setting is invalid",
            error_code="OPS_NOTIFICATION_CONFIG_INVALID",
        )
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise StateError(
            "notification numeric setting is invalid",
            error_code="OPS_NOTIFICATION_CONFIG_INVALID",
        ) from exc
    if not minimum <= result <= maximum:
        raise StateError(
            "notification numeric setting is outside its range",
            error_code="OPS_NOTIFICATION_CONFIG_INVALID",
        )
    return result


def _bounded_float(
    value: Any,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool):
        raise StateError(
            "notification numeric setting is invalid",
            error_code="OPS_NOTIFICATION_CONFIG_INVALID",
        )
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise StateError(
            "notification numeric setting is invalid",
            error_code="OPS_NOTIFICATION_CONFIG_INVALID",
        ) from exc
    if not minimum <= result <= maximum:
        raise StateError(
            "notification numeric setting is outside its range",
            error_code="OPS_NOTIFICATION_CONFIG_INVALID",
        )
    return result
