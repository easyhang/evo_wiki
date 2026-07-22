from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from evo_wiki.state import StateError, StateStore
from evo_wiki.state.notifications import (
    NotificationDispatcher,
    build_notification,
    notification_settings,
)

from test_cli_smoke import run_cli


class _WebhookHandler(BaseHTTPRequestHandler):
    responses: list[int] = []
    received: list[dict[str, Any]] = []
    redirect_url: str | None = None

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.__class__.received.append(
            {
                "body": body,
                "event_id": self.headers.get("X-Evo-Event-ID"),
                "timestamp": self.headers.get("X-Evo-Timestamp"),
                "signature": self.headers.get("X-Evo-Signature"),
            }
        )
        status = (
            self.__class__.responses.pop(0)
            if self.__class__.responses
            else 204
        )
        self.send_response(status)
        if 300 <= status < 400 and self.__class__.redirect_url:
            self.send_header("Location", self.__class__.redirect_url)
        self.end_headers()
        self.wfile.write(b"private webhook response")

    def log_message(self, *_: Any) -> None:
        return


@pytest.fixture
def webhook_server():
    _WebhookHandler.responses = []
    _WebhookHandler.received = []
    _WebhookHandler.redirect_url = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _notification_config(
    webhook_server: ThreadingHTTPServer,
) -> tuple[dict[str, Any], dict[str, str]]:
    project = {
        "operations": {
            "notifications": {
                "enabled": True,
                "webhook_url_env": "TEST_WEBHOOK_URL",
                "signing_key_env": "TEST_WEBHOOK_KEY",
                "max_attempts": 3,
                "initial_backoff_seconds": 0,
                "max_backoff_seconds": 0,
            }
        }
    }
    environ = {
        "TEST_WEBHOOK_URL": (
            f"http://127.0.0.1:{webhook_server.server_port}/events"
        ),
        "TEST_WEBHOOK_KEY": "k" * 32,
    }
    return project, environ


def test_notification_outbox_retries_signs_and_does_not_advance_sequence(
    tmp_path: Path,
    webhook_server: ThreadingHTTPServer,
):
    project = tmp_path / "workspace"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    store = StateStore(project)
    config, environ = _notification_config(webhook_server)
    settings = notification_settings(config, environ=environ)
    before = store.state_commit_seq()
    event = build_notification(
        root=project,
        event_type="AUDIT_OPENED",
        severity="HIGH",
        subject_type="audit_item",
        subject_id="audit-test",
        dedupe_key="AUDIT_OPENED:audit-test",
        security_domain="default",
        state="OPEN",
        error_code="QUERY_REFERENCE_UNMAPPED",
        counts={"reference_count": 1},
        max_attempts=3,
    )
    first = store.enqueue_notification(event)
    duplicate = store.enqueue_notification(event)
    assert first["id"] == duplicate["id"]
    assert store.state_commit_seq() == before

    _WebhookHandler.responses = [503, 204]
    dispatcher = NotificationDispatcher(
        store,
        settings,
        worker_id="worker-test",
    )
    retried = dispatcher.dispatch_due()
    delivered = dispatcher.dispatch_due()

    assert retried["status"] == "retry_wait"
    assert delivered["status"] == "delivered"
    terminal = store.notification_status(first["id"])
    assert terminal["status"] == "DELIVERED"
    assert [item["outcome"] for item in terminal["attempts"]] == [
        "RETRYABLE",
        "DELIVERED",
    ]
    assert store.state_commit_seq() == before
    assert len(_WebhookHandler.received) == 2
    assert {
        request["event_id"] for request in _WebhookHandler.received
    } == {first["id"]}
    for request in _WebhookHandler.received:
        payload = json.loads(request["body"])
        assert request["event_id"] == payload["event_id"]
        expected = hmac.new(
            b"k" * 32,
            request["timestamp"].encode("utf-8")
            + b"."
            + request["body"],
            hashlib.sha256,
        ).hexdigest()
        assert request["signature"] == f"v1={expected}"
        wrong_key_signature = hmac.new(
            b"x" * 32,
            request["timestamp"].encode("utf-8")
            + b"."
            + request["body"],
            hashlib.sha256,
        ).hexdigest()
        assert request["signature"] != f"v1={wrong_key_signature}"
        assert "query" not in payload
        assert "answer" not in payload
    database = store.database_path.read_bytes()
    assert environ["TEST_WEBHOOK_URL"].encode() not in database
    assert b"private webhook response" not in database
    assert b"k" * 32 not in database


def test_notification_claim_expiry_is_durable_and_retry_is_explicit(
    tmp_path: Path,
):
    project = tmp_path / "workspace"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    store = StateStore(project)
    event = build_notification(
        root=project,
        event_type="MAINTENANCE_DRAINING",
        severity="CRITICAL",
        subject_type="replacement_operation",
        subject_id="operation-test",
        dedupe_key="MAINTENANCE_DRAINING:operation-test:DRAINING",
        security_domain="default",
        state="DRAINING",
        delivery_required=True,
        max_attempts=1,
    )
    item = store.enqueue_notification(event)
    claimed = store.claim_due_notifications(
        worker_id="crashed-worker",
        limit=1,
        claim_seconds=0.1,
    )
    assert claimed[0]["id"] == item["id"]
    assert (
        store.claim_due_notifications(
            worker_id="second-worker",
            limit=1,
        )
        == []
    )
    time.sleep(0.2)
    assert (
        store.claim_due_notifications(
            worker_id="recovery-worker",
            limit=1,
        )
        == []
    )
    failed = store.notification_status(item["id"])
    assert failed["status"] == "FAILED"
    assert failed["attempts"][0]["error_code"] == (
        "OPS_NOTIFICATION_CLAIM_EXPIRED"
    )

    pending = store.retry_notification(
        item["id"],
        additional_attempts=2,
    )
    assert pending["status"] == "PENDING"
    assert pending["attempt_count"] == 1
    assert pending["max_attempts"] == 3


def test_notification_configuration_rejects_insecure_remote_http():
    with pytest.raises(StateError) as caught:
        notification_settings(
            {
                "operations": {
                    "notifications": {
                        "enabled": True,
                    }
                }
            },
            environ={
                "EVO_WIKI_OPS_WEBHOOK_URL": (
                    "http://example.com/webhook"
                ),
                "EVO_WIKI_OPS_WEBHOOK_KEY": "k" * 32,
            },
        )
    assert caught.value.error_code == (
        "OPS_NOTIFICATION_WEBHOOK_INSECURE"
    )


def test_notification_redirect_is_terminal_and_not_followed(
    tmp_path: Path,
    webhook_server: ThreadingHTTPServer,
):
    project = tmp_path / "workspace"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    store = StateStore(project)
    config, environ = _notification_config(webhook_server)
    settings = notification_settings(config, environ=environ)
    event = build_notification(
        root=project,
        event_type="AUDIT_OPENED",
        severity="HIGH",
        subject_type="audit_item",
        subject_id="audit-redirect",
        dedupe_key="AUDIT_OPENED:audit-redirect",
        security_domain="default",
    )
    item = store.enqueue_notification(event)
    _WebhookHandler.redirect_url = (
        f"http://127.0.0.1:{webhook_server.server_port}/redirected"
    )
    _WebhookHandler.responses = [302]

    result = NotificationDispatcher(
        store,
        settings,
        worker_id="redirect-worker",
    ).dispatch_due()

    assert result["status"] == "failed"
    assert len(_WebhookHandler.received) == 1
    terminal = store.notification_status(item["id"])
    assert terminal["status"] == "FAILED"
    assert terminal["attempts"][0]["http_status_class"] == "3XX"
    assert terminal["attempts"][0]["error_code"] == (
        "OPS_NOTIFICATION_REMOTE_REJECTED"
    )


def test_expired_query_lease_requires_matching_cli_confirmation(
    tmp_path: Path,
):
    project = tmp_path / "workspace"
    initialized = run_cli(tmp_path, "init", "--root", str(project))
    assert initialized.returncode == 0, initialized.stderr
    store = StateStore(project)
    partition_id, _ = store.ensure_partition(
        {
            "mode": "service",
            "base_url": "http://127.0.0.1:9621",
            "workspace": "lease_test",
            "embedding": {"batch_size": 8},
        }
    )
    store.begin_query_run(
        request_id="query-expired",
        retrieval_partition_id=partition_id,
        principal_hmac="hmac-principal",
        query_hmac="hmac-query",
        request_mode="mix",
        gateway_mode="enforce",
        verification_level="test",
        lease_seconds=0.1,
    )
    time.sleep(1.1)

    mismatch = run_cli(
        tmp_path,
        "gateway",
        "lease-recover",
        "--root",
        str(project),
        "--request-id",
        "query-expired",
        "--action",
        "abandon",
        "--confirm",
        "wrong",
        "--json",
    )
    recovered = run_cli(
        tmp_path,
        "gateway",
        "lease-recover",
        "--root",
        str(project),
        "--request-id",
        "query-expired",
        "--action",
        "abandon",
        "--confirm",
        "query-expired",
        "--json",
    )

    assert mismatch.returncode == 5
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout)["query_status"] == "ABANDONED"
