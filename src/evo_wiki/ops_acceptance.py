"""Isolated QG-001 production-operations acceptance orchestration."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import EvoConfig
from .lightrag_lane import (
    LightRAGServiceClient,
    resolve_lightrag_service_config,
)
from .state import StateSchemaMigrator, StateStore, StateVerifier
from .state.contracts import StateError
from .utils import read_json, write_json, write_json_atomic


CARD = "QG-001-OPS-ACCEPTANCE"
LIGHTRAG_IMAGE = "ghcr.io/hkuds/lightrag:latest"
NGINX_IMAGE = "nginx:1.27-alpine"
LABEL_CARD = "io.evo-wiki.acceptance"
LABEL_RUN = "io.evo-wiki.acceptance-run"


class AcceptanceError(StateError):
    def __init__(
        self,
        error_code: str,
        *,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            "QG-001 operations acceptance failed",
            error_code=error_code,
            details=details,
        )


@dataclass
class _WebhookState:
    signing_key: bytes
    fail: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class _ProxyState:
    target_url: str
    mutate_reference: bool = False
    query_delay_seconds: float = 0
    calls: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def count(self, method: str, path: str) -> int:
        with self.lock:
            return sum(
                1
                for item in self.calls
                if item["method"] == method and item["path"] == path
            )


@dataclass
class _GatewayRelayState:
    target_url: str
    token: str


class QG001AcceptanceService:
    def __init__(
        self,
        *,
        source_root: Path,
        report_path: Path,
        provider_env_file: Path | None,
        allow_image_pull: bool,
    ):
        self.source_root = source_root.expanduser().resolve()
        self.report_path = report_path.expanduser().resolve()
        self.provider_env_file = (
            provider_env_file.expanduser().resolve()
            if provider_env_file is not None
            else None
        )
        self.allow_image_pull = allow_image_pull

    def plan(self) -> dict[str, Any]:
        blockers: list[str] = []
        if not self.source_root.is_dir():
            blockers.append("QG_ACCEPTANCE_SOURCE_MISSING")
        try:
            self.report_path.relative_to(self.source_root)
        except ValueError:
            pass
        else:
            blockers.append("QG_ACCEPTANCE_REPORT_INSIDE_SOURCE")
        docker_ready = _command_ok(["docker", "info"])
        if not docker_ready:
            blockers.append("QG_ACCEPTANCE_DOCKER_UNAVAILABLE")
        lightrag_image = _docker_image_present(LIGHTRAG_IMAGE)
        nginx_image = _docker_image_present(NGINX_IMAGE)
        if not lightrag_image:
            blockers.append("QG_ACCEPTANCE_LIGHTRAG_IMAGE_MISSING")
        if not nginx_image and not self.allow_image_pull:
            blockers.append("QG_ACCEPTANCE_NGINX_IMAGE_MISSING")
        if shutil.which("htpasswd") is None:
            blockers.append("QG_ACCEPTANCE_HTPASSWD_MISSING")
        try:
            import starlette  # noqa: F401
            import uvicorn  # noqa: F401
        except ImportError:
            blockers.append("QG_ACCEPTANCE_GATEWAY_DEPENDENCY_MISSING")

        source = self._source_summary() if self.source_root.is_dir() else {}
        return {
            "schema_version": 1,
            "card": CARD,
            "run_id": None,
            "status": "blocked" if blockers else "ready",
            "mode": "dry_run",
            "source_workspace_mutated": False,
            "prerequisites": {
                "docker": docker_ready,
                "lightrag_image": lightrag_image,
                "nginx_image": nginx_image,
                "nginx_pull_authorized": self.allow_image_pull,
                "htpasswd": shutil.which("htpasswd") is not None,
                "provider_env_required_for_apply": True,
            },
            "source_guard": source,
            "blockers": blockers,
            "workspace_mutated": False,
            "error_code": blockers[0] if blockers else None,
        }

    def apply(self, *, confirm: str) -> dict[str, Any]:
        if confirm != CARD:
            raise StateError(
                "acceptance confirmation does not match",
                error_code="QG_ACCEPTANCE_CONFIRMATION_MISMATCH",
            )
        if (
            self.provider_env_file is None
            or not self.provider_env_file.is_file()
        ):
            raise StateError(
                "acceptance provider env-file is required",
                error_code="QG_ACCEPTANCE_PROVIDER_CONFIG_MISSING",
            )
        preview = self.plan()
        if preview["status"] == "blocked":
            raise StateError(
                "acceptance prerequisites are not satisfied",
                error_code=str(preview["error_code"]),
            )

        run_id = f"qg001-{uuid.uuid4().hex[:16]}"
        runtime = Path(
            tempfile.mkdtemp(prefix=f"evo-wiki-{run_id}-")
        ).resolve()
        source_before = _workspace_manifest(self.source_root)
        provider_before = _file_guard(self.provider_env_file)
        containers: list[str] = []
        networks: list[str] = []
        stages: dict[str, Any] = {}
        result: dict[str, Any]
        try:
            if not _docker_image_present(NGINX_IMAGE):
                if not self.allow_image_pull:
                    raise AcceptanceError(
                        "QG_ACCEPTANCE_NGINX_IMAGE_MISSING"
                    )
                _run_checked(
                    ["docker", "pull", NGINX_IMAGE],
                    error_code="QG_ACCEPTANCE_NGINX_PULL_FAILED",
                    timeout=300,
                )
            stages["migration"] = self._accept_copy_migration(runtime)
            isolated = self._accept_isolated_runtime(
                runtime,
                run_id=run_id,
                containers=containers,
                networks=networks,
            )
            stages.update(isolated)
            source_after = _workspace_manifest(self.source_root)
            provider_after = _file_guard(self.provider_env_file)
            source_unchanged = source_before == source_after
            provider_unchanged = provider_before == provider_after
            if not source_unchanged:
                raise AcceptanceError(
                    "QG_ACCEPTANCE_SOURCE_WORKSPACE_MUTATED"
                )
            if not provider_unchanged:
                raise AcceptanceError(
                    "QG_ACCEPTANCE_PROVIDER_CONFIG_MUTATED"
                )
            source_final = self._source_summary()
            if not _source_remote_is_expected(source_final):
                raise AcceptanceError(
                    "QG_ACCEPTANCE_SOURCE_REMOTE_DRIFT"
                )
            result = {
                "schema_version": 1,
                "card": CARD,
                "run_id": run_id,
                "status": "passed",
                "mode": "isolated_apply",
                "source_workspace_mutated": False,
                "source_guard": {
                    **source_final,
                    "manifest_unchanged": True,
                    "provider_config_unchanged": True,
                },
                **stages,
                "cleanup": {},
                "workspace_mutated": True,
                "error_code": None,
            }
        except StateError as exc:
            error_code = exc.error_code
            result = {
                "schema_version": 1,
                "card": CARD,
                "run_id": run_id,
                "status": "failed",
                "mode": "isolated_apply",
                "source_workspace_mutated": (
                    source_before != _workspace_manifest(self.source_root)
                ),
                "source_guard": self._source_summary(),
                **stages,
                "cleanup": {},
                "workspace_mutated": True,
                "failure_details": exc.details,
                "error_code": error_code,
            }
        except Exception:
            result = {
                "schema_version": 1,
                "card": CARD,
                "run_id": run_id,
                "status": "failed",
                "mode": "isolated_apply",
                "source_workspace_mutated": (
                    source_before != _workspace_manifest(self.source_root)
                ),
                "source_guard": self._source_summary(),
                **stages,
                "cleanup": {},
                "workspace_mutated": True,
                "error_code": "QG_ACCEPTANCE_UNEXPECTED_FAILURE",
            }
        finally:
            cleanup = _cleanup_runtime(
                runtime=runtime,
                containers=containers,
                networks=networks,
            )
        result["cleanup"] = cleanup
        if not cleanup["complete"] and result["status"] == "passed":
            result["status"] = "failed"
            result["error_code"] = "QG_ACCEPTANCE_CLEANUP_FAILED"
        write_json_atomic(self.report_path, result)
        return result

    def _source_summary(self) -> dict[str, Any]:
        config = EvoConfig.load(self.source_root)
        store = StateStore(
            self.source_root,
            config.project.get("state", {}),
        )
        inspected = store.inspect()
        remote = _remote_summary(config.project.get("lightrag", {}))
        database_stat = store.database_path.stat()
        return {
            "database_schema_version": int(
                inspected["schema_version"]
            ),
            "state_commit_seq": int(inspected["state_commit_seq"]),
            "database_sha256": _sha256_file(store.database_path),
            "database_size_bytes": int(database_stat.st_size),
            "database_mtime_ns": int(database_stat.st_mtime_ns),
            "workspace_manifest_sha256": _workspace_manifest(
                self.source_root
            ),
            "remote": remote,
        }

    def _accept_copy_migration(self, runtime: Path) -> dict[str, Any]:
        copied = runtime / "source-copy"
        shutil.copytree(self.source_root, copied)
        config = EvoConfig.load(copied)
        store = StateStore(copied, config.project.get("state", {}))
        before = store.schema_version()
        applied = StateSchemaMigrator(
            store,
            config.project.get("journal", {}),
        ).apply()
        repeated = StateSchemaMigrator(
            store,
            config.project.get("journal", {}),
        ).apply()
        verification = StateVerifier(store).verify(
            include_journal=False
        )
        if (
            applied.status not in {"applied", "already_applied"}
            or repeated.status != "already_applied"
            or verification.overall_status != "PASS"
            or store.schema_version() != 5
        ):
            raise AcceptanceError(
                "QG_ACCEPTANCE_COPY_MIGRATION_FAILED"
            )
        copied_project = read_json(copied / "project.json", {})
        copied_project["query_gateway"] = {
            **(copied_project.get("query_gateway") or {}),
            "mode": "shadow",
            "listen": "127.0.0.1:18765",
        }
        write_json(copied / "project.json", copied_project)
        copied_config = EvoConfig.load(copied)
        from .query_gateway import TrustedQueryGateway

        check = TrustedQueryGateway(
            store,
            copied_config.project,
            audit_key=b"qg001-copy-check-key-32-bytes!!",
        ).check()
        if check["status"] != "ready":
            raise AcceptanceError(
                "QG_ACCEPTANCE_COPY_GATEWAY_CHECK_FAILED"
            )
        return {
            "status": "passed",
            "database_schema_before": before,
            "database_schema_after": store.schema_version(),
            "repeated_upgrade_noop": True,
            "verify": "PASS",
            "gateway_check": "ready",
        }

    def _accept_isolated_runtime(
        self,
        runtime: Path,
        *,
        run_id: str,
        containers: list[str],
        networks: list[str],
    ) -> dict[str, Any]:
        assert self.provider_env_file is not None
        network = f"evo-{run_id}"
        _run_checked(
            [
                "docker",
                "network",
                "create",
                "--label",
                f"{LABEL_CARD}={CARD}",
                "--label",
                f"{LABEL_RUN}={run_id}",
                network,
            ],
            error_code="QG_ACCEPTANCE_NETWORK_CREATE_FAILED",
        )
        networks.append(network)
        storage = runtime / "lightrag-storage"
        inputs = runtime / "lightrag-inputs"
        prompts = runtime / "lightrag-prompts"
        for path in (storage, inputs, prompts):
            path.mkdir(parents=True, exist_ok=True)
        lightrag_name = f"evo-{run_id}-lightrag"
        workspace = f"qg001_{run_id.replace('-', '_')}"
        _run_checked(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                lightrag_name,
                "--network",
                network,
                "--label",
                f"{LABEL_CARD}={CARD}",
                "--label",
                f"{LABEL_RUN}={run_id}",
                "-p",
                "127.0.0.1::9621",
                "--env-file",
                str(self.provider_env_file),
                "-e",
                "HOST=0.0.0.0",
                "-e",
                "PORT=9621",
                "-e",
                f"WORKSPACE={workspace}",
                "-e",
                "LIGHTRAG_KV_STORAGE=JsonKVStorage",
                "-e",
                "LIGHTRAG_DOC_STATUS_STORAGE=JsonDocStatusStorage",
                "-e",
                "LIGHTRAG_GRAPH_STORAGE=NetworkXStorage",
                "-e",
                "LIGHTRAG_VECTOR_STORAGE=NanoVectorDBStorage",
                "-v",
                f"{storage}:/app/data/rag_storage",
                "-v",
                f"{inputs}:/app/data/inputs",
                "-v",
                f"{prompts}:/app/data/prompts",
                "-v",
                f"{self.provider_env_file}:/app/.env:ro",
                LIGHTRAG_IMAGE,
            ],
            error_code="QG_ACCEPTANCE_LIGHTRAG_START_FAILED",
        )
        containers.append(lightrag_name)
        lightrag_port = _docker_host_port(lightrag_name, 9621)
        lightrag_url = f"http://127.0.0.1:{lightrag_port}"
        _wait_json_health(
            lightrag_url,
            "/health",
            timeout_seconds=180,
            error_code="QG_ACCEPTANCE_LIGHTRAG_UNHEALTHY",
        )

        proxy_state = _ProxyState(lightrag_url)
        proxy_server, proxy_thread = _start_proxy(proxy_state)
        webhook_key_text = secrets.token_urlsafe(32)
        webhook_key = webhook_key_text.encode("ascii")
        webhook_state = _WebhookState(webhook_key)
        webhook_server, webhook_thread = _start_webhook(webhook_state)
        gateway_relay_server: ThreadingHTTPServer | None = None
        gateway_relay_thread: threading.Thread | None = None
        gateway_holder: dict[str, subprocess.Popen[bytes] | None] = {
            "process": None
        }
        nginx_name: str | None = None
        try:
            proxy_url = (
                f"http://127.0.0.1:{proxy_server.server_port}"
            )
            webhook_url = (
                f"http://127.0.0.1:{webhook_server.server_port}/events"
            )
            synthetic = self._create_synthetic_workspace(
                runtime,
                workspace=workspace,
                proxy_url=proxy_url,
                webhook_url=webhook_url,
                webhook_key=webhook_key,
            )
            relay_token = secrets.token_hex(32)
            gateway_relay_server, gateway_relay_thread = (
                _start_gateway_relay(
                    _GatewayRelayState(
                        target_url=(
                            "http://127.0.0.1:"
                            f"{_gateway_port(synthetic)}"
                        ),
                        token=relay_token,
                    )
                )
            )
            env = {
                **os.environ,
                "EVO_WIKI_QUERY_AUDIT_KEY": (
                    "qg001-query-audit-key-32-bytes!"
                ),
                "EVO_WIKI_OPS_WEBHOOK_URL": webhook_url,
                "EVO_WIKI_OPS_WEBHOOK_KEY": webhook_key_text,
            }
            _run_evo(
                [
                    "run",
                    "--root",
                    str(synthetic),
                    "--lane",
                    "lightrag",
                    "--json",
                ],
                env=env,
                allowed={0},
                error_code="QG_ACCEPTANCE_INITIAL_INDEX_FAILED",
                timeout=300,
            )
            _run_evo(
                [
                    "run",
                    "--root",
                    str(synthetic),
                    "--lane",
                    "wiki",
                    "--json",
                ],
                env=env,
                allowed={0},
                error_code="QG_ACCEPTANCE_WIKI_BUILD_FAILED",
                timeout=120,
            )
            _run_evo(
                ["export-platform", "--root", str(synthetic)],
                env=env,
                allowed={0},
                error_code="QG_ACCEPTANCE_PLATFORM_EXPORT_FAILED",
            )
            username = f"accept-{secrets.token_hex(4)}"
            password = secrets.token_urlsafe(18)
            platform = synthetic / "artifacts" / "platform"
            nginx_config = platform / "nginx.conf"
            nginx_config.write_text(
                nginx_config.read_text(encoding="utf-8").replace(
                    f"http://127.0.0.1:{_gateway_port(synthetic)}",
                    (
                        "http://host.docker.internal:"
                        f"{gateway_relay_server.server_port}"
                    ),
                ).replace(
                    "proxy_set_header Host $host;",
                    "proxy_set_header Host $host;\n"
                    "      proxy_set_header "
                    f"X-Evo-Acceptance-Relay {relay_token};",
                ),
                encoding="utf-8",
            )
            (platform / "conf").mkdir(mode=0o700)
            _run_checked(
                [
                    "htpasswd",
                    "-bc",
                    str(platform / "conf" / "htpasswd"),
                    username,
                    password,
                ],
                error_code="QG_ACCEPTANCE_HTPASSWD_FAILED",
            )
            gateway_holder["process"] = _start_gateway(synthetic, env)
            gateway_port = _gateway_port(synthetic)
            _wait_json_health(
                f"http://127.0.0.1:{gateway_port}",
                "/internal/readyz",
                timeout_seconds=60,
                error_code="QG_ACCEPTANCE_GATEWAY_UNREADY",
            )
            nginx_name = f"evo-{run_id}-nginx"
            _run_checked(
                [
                    "docker",
                    "run",
                    "-d",
                    "--rm",
                    "--name",
                    nginx_name,
                    "--label",
                    f"{LABEL_CARD}={CARD}",
                    "--label",
                    f"{LABEL_RUN}={run_id}",
                    "--add-host",
                    "host.docker.internal:host-gateway",
                    "-p",
                    "127.0.0.1::8080",
                    "-v",
                    f"{platform}:/srv/platform:ro",
                    NGINX_IMAGE,
                    "nginx",
                    "-p",
                    "/srv/platform/",
                    "-c",
                    "nginx.conf",
                    "-g",
                    "daemon off;",
                ],
                error_code="QG_ACCEPTANCE_NGINX_START_FAILED",
            )
            containers.append(nginx_name)
            nginx_port = _docker_host_port(nginx_name, 8080)
            public_url = f"http://127.0.0.1:{nginx_port}"
            _wait_http_status(
                public_url,
                "/",
                expected_status=200,
                timeout_seconds=30,
                error_code="QG_ACCEPTANCE_NGINX_UNREADY",
            )
            auth = self._accept_auth(
                public_url,
                username=username,
                password=password,
            )
            shadow = self._accept_shadow(
                public_url,
                username=username,
                password=password,
                proxy_state=proxy_state,
                synthetic=synthetic,
                webhook_state=webhook_state,
            )
            _stop_process(gateway_holder["process"])
            gateway_holder["process"] = None
            project = read_json(synthetic / "project.json", {})
            project["query_gateway"]["mode"] = "enforce"
            write_json(synthetic / "project.json", project)
            gateway_holder["process"] = _start_gateway(synthetic, env)
            _wait_json_health(
                f"http://127.0.0.1:{gateway_port}",
                "/internal/readyz",
                timeout_seconds=60,
                error_code="QG_ACCEPTANCE_GATEWAY_UNREADY",
            )
            enforce = self._accept_enforce_audit(
                public_url,
                username=username,
                password=password,
                proxy_state=proxy_state,
                synthetic=synthetic,
                env=env,
            )
            replacement = self._accept_replacement(
                public_url,
                username=username,
                password=password,
                proxy_state=proxy_state,
                webhook_state=webhook_state,
                synthetic=synthetic,
                env=env,
                stop_gateway=lambda: _replace_process(
                    gateway_holder,
                    None,
                ),
                start_gateway=lambda: _replace_process(
                    gateway_holder,
                    _start_gateway(synthetic, env),
                ),
                gateway_port=gateway_port,
            )
            if (
                not webhook_state.events
                or not all(
                    bool(item["signature_valid"])
                    and bool(item["payload_safe"])
                    for item in webhook_state.events
                )
                or not _has_retried_event_id(webhook_state.events)
            ):
                raise AcceptanceError(
                    "QG_ACCEPTANCE_WEBHOOK_CONTRACT_FAILED"
                )
            final_remote = _remote_summary(
                EvoConfig.load(synthetic).project.get("lightrag", {})
            )
            return {
                "auth": auth,
                "shadow": shadow,
                "enforce": enforce,
                "audit": enforce["audit"],
                "webhook": {
                    "delivered_events": sum(
                        1
                        for item in webhook_state.events
                        if item["accepted"]
                    ),
                    "signatures_valid": all(
                        bool(item["signature_valid"])
                        for item in webhook_state.events
                    ),
                    "retry_event_id_stable": _has_retried_event_id(
                        webhook_state.events
                    ),
                    "payload_redaction": (
                        "PASS"
                        if all(
                            bool(item["payload_safe"])
                            for item in webhook_state.events
                        )
                        else "FAIL"
                    ),
                },
                "lease": replacement["lease"],
                "maintenance": replacement["maintenance"],
                "replacement": replacement["replacement"],
                "counts": replacement["counts"],
                "remote_final": final_remote,
                "runtime": {
                    "schema_version": 5,
                    "lightrag_image_digest": _docker_image_digest(
                        LIGHTRAG_IMAGE
                    ),
                    "nginx_image_digest": _docker_image_digest(
                        NGINX_IMAGE
                    ),
                },
            }
        finally:
            _stop_process(gateway_holder["process"])
            proxy_server.shutdown()
            webhook_server.shutdown()
            proxy_server.server_close()
            webhook_server.server_close()
            proxy_thread.join(timeout=2)
            webhook_thread.join(timeout=2)
            if gateway_relay_server is not None:
                gateway_relay_server.shutdown()
                gateway_relay_server.server_close()
            if gateway_relay_thread is not None:
                gateway_relay_thread.join(timeout=2)

    def _create_synthetic_workspace(
        self,
        runtime: Path,
        *,
        workspace: str,
        proxy_url: str,
        webhook_url: str,
        webhook_key: bytes,
    ) -> Path:
        root = runtime / "synthetic"
        _run_evo(
            ["init", "--root", str(root)],
            env=dict(os.environ),
            allowed={0},
            error_code="QG_ACCEPTANCE_SYNTHETIC_INIT_FAILED",
        )
        source = root / "corpus" / "raw" / "acceptance.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "QG-001验收文档明确记载：系统基准年份为2026年，"
            "维护编号为OPS-2026。",
            encoding="utf-8",
        )
        project = read_json(root / "project.json", {})
        project["lightrag"].update(
            {
                "mode": "service",
                "base_url": proxy_url,
                "workspace": workspace,
                "timeout_seconds": 120,
                "sync": {
                    "poll_interval_seconds": 0.5,
                    "poll_timeout_seconds": 180,
                },
                "replacement": {
                    "enabled": True,
                    "maintenance_window_seconds": 120,
                    "absence_confirmations": 2,
                    "auto_compensate": True,
                },
                "embedding": {"batch_size": 8},
            }
        )
        gateway_port = _free_port()
        project["query_gateway"].update(
            {
                "mode": "shadow",
                "listen": f"127.0.0.1:{gateway_port}",
                "request_timeout_seconds": 120,
                "drain_timeout_seconds": 15,
            }
        )
        project["security"].update(
            {
                "auth_mode": "trusted_proxy",
                "principal_header": "X-Evo-Principal",
                "default_domain": "default",
                "fail_closed": True,
            }
        )
        project["operations"]["notifications"].update(
            {
                "enabled": True,
                "webhook_url_env": "EVO_WIKI_OPS_WEBHOOK_URL",
                "signing_key_env": "EVO_WIKI_OPS_WEBHOOK_KEY",
                "max_attempts": 3,
                "request_timeout_seconds": 2,
                "initial_backoff_seconds": 0.1,
                "max_backoff_seconds": 0.2,
                "dispatch_interval_seconds": 0.1,
                "required_delivery_timeout_seconds": 3,
                "maintenance_delivery_required": True,
            }
        )
        write_json(root / "project.json", project)
        return root

    def _accept_auth(
        self,
        public_url: str,
        *,
        username: str,
        password: str,
    ) -> dict[str, Any]:
        no_auth, _ = _http_json(
            public_url,
            "POST",
            "/api/query",
            {"schema_version": 2, "query": "基准年份是什么？"},
        )
        wrong, _ = _http_json(
            public_url,
            "POST",
            "/api/query",
            {"schema_version": 2, "query": "基准年份是什么？"},
            basic_auth=("wrong", "wrong"),
        )
        direct = {
            path: _http_json(public_url, "GET", path)[0]
            for path in (
                "/query",
                "/documents/paginated",
                "/health",
                "/openapi.json",
            )
        }
        if no_auth != 401 or wrong != 401 or any(
            status != 404 for status in direct.values()
        ):
            raise AcceptanceError("QG_ACCEPTANCE_AUTH_FAILED")
        return {
            "unauthenticated_status": no_auth,
            "wrong_credentials_status": wrong,
            "direct_lightrag_denied": True,
            "identity_header_overwritten": True,
            "basic_auth": "passed",
        }

    def _accept_shadow(
        self,
        public_url: str,
        *,
        username: str,
        password: str,
        proxy_state: _ProxyState,
        synthetic: Path,
        webhook_state: _WebhookState,
    ) -> dict[str, Any]:
        proxy_state.mutate_reference = True
        status, payload = _http_json(
            public_url,
            "POST",
            "/api/query",
            {
                "schema_version": 2,
                "query": "系统基准年份是什么？",
                "mode": "mix",
                "top_k": 20,
            },
            basic_auth=(username, password),
            headers={"X-Evo-Principal": "spoofed"},
            timeout=150,
        )
        proxy_state.mutate_reference = False
        if (
            status != 200
            or payload.get("generation_status") != "succeeded"
            or not payload.get("answer")
            or payload.get("answer_origin") != "general_model"
            or payload.get("evidence_status") != "ungrounded"
            or payload.get("review_status") != "pending"
        ):
            raise AcceptanceError("QG_ACCEPTANCE_SHADOW_DELIVERY_FAILED")
        store = StateStore(synthetic)
        deadline = time.monotonic() + 10
        while (
            not webhook_state.events
            and time.monotonic() < deadline
        ):
            time.sleep(0.1)
        if not webhook_state.events:
            raise AcceptanceError(
                "QG_ACCEPTANCE_AUDIT_WEBHOOK_MISSING"
            )
        connection = store.connect(read_only=True)
        try:
            principal_hmac = connection.execute(
                """
                SELECT principal_hmac FROM query_run
                ORDER BY created_at DESC, id DESC LIMIT 1
                """
            ).fetchone()[0]
        finally:
            connection.close()
        expected = "hmac-sha256:" + hmac.new(
            b"qg001-query-audit-key-32-bytes!",
            username.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if principal_hmac != expected:
            raise AcceptanceError(
                "QG_ACCEPTANCE_IDENTITY_OVERWRITE_FAILED"
            )
        return {
            "status": "passed",
            "delivery_status": "succeeded",
            "answer_origin": "general_model",
            "evidence_status": "ungrounded",
            "answer_released": True,
            "audit_created": bool(payload.get("audit_id")),
        }

    def _accept_enforce_audit(
        self,
        public_url: str,
        *,
        username: str,
        password: str,
        proxy_state: _ProxyState,
        synthetic: Path,
        env: dict[str, str],
    ) -> dict[str, Any]:
        proxy_state.mutate_reference = True
        status, payload = _http_json(
            public_url,
            "POST",
            "/api/query",
            {
                "schema_version": 2,
                "query": "系统基准年份是什么？",
                "mode": "mix",
                "top_k": 20,
            },
            basic_auth=(username, password),
            timeout=150,
        )
        proxy_state.mutate_reference = False
        if (
            status != 200
            or payload.get("generation_status") != "succeeded"
            or not payload.get("answer")
            or payload.get("answer_origin") != "general_model"
            or payload.get("evidence_status") != "ungrounded"
            or payload.get("review_status") != "pending"
        ):
            raise AcceptanceError("QG_ACCEPTANCE_ENFORCE_FAILED")
        audit_id = payload.get("audit_id")
        if not isinstance(audit_id, str):
            raise AcceptanceError("QG_ACCEPTANCE_AUDIT_ID_MISSING")
        store = StateStore(synthetic)
        shown = _run_evo_json(
            [
                "audit",
                "show",
                "--root",
                str(synthetic),
                "--audit-id",
                audit_id,
                "--include-content",
                "--json",
            ],
            env=env,
            allowed={0},
            error_code="QG_ACCEPTANCE_AUDIT_SHOW_FAILED",
        )
        content = shown.get("item", {}).get("content", {})
        if (
            content.get("answer") != payload.get("answer")
            or content.get("question") != "系统基准年份是什么？"
        ):
            raise AcceptanceError(
                "QG_ACCEPTANCE_AUDIT_CONTENT_INVALID"
            )
        before = store.state_commit_seq()
        resolved = _run_evo_json(
            [
                "audit",
                "resolve",
                "--root",
                str(synthetic),
                "--audit-id",
                audit_id,
                "--confirm",
                audit_id,
                "--resolution",
                "APPROVED",
                "--json",
            ],
            env=env,
            allowed={0},
            error_code="QG_ACCEPTANCE_AUDIT_RESOLVE_FAILED",
        )
        after = store.state_commit_seq()
        if after != before + 1:
            raise AcceptanceError(
                "QG_ACCEPTANCE_AUDIT_SEQUENCE_INVALID"
            )
        if not resolved.get("payload_deleted"):
            raise AcceptanceError(
                "QG_ACCEPTANCE_AUDIT_PAYLOAD_NOT_DELETED"
            )
        return {
            "status": "passed",
            "delivery_status": "succeeded",
            "answer_origin": "general_model",
            "evidence_status": "ungrounded",
            "answer_released": True,
            "audit": {
                "opened": True,
                "resolved": True,
                "content_verified": True,
                "payload_deleted": True,
                "state_commit_seq_delta": 1,
            },
        }

    def _accept_replacement(
        self,
        public_url: str,
        *,
        username: str,
        password: str,
        proxy_state: _ProxyState,
        webhook_state: _WebhookState,
        synthetic: Path,
        env: dict[str, str],
        stop_gateway: Any,
        start_gateway: Any,
        gateway_port: int,
    ) -> dict[str, Any]:
        source = synthetic / "corpus" / "raw" / "acceptance.md"
        all_submits_before = proxy_state.count(
            "POST",
            "/documents/text",
        )
        source.write_text(
            "QG-001验收文档更新：系统基准年份为2027年，"
            "维护编号为OPS-2027。",
            encoding="utf-8",
        )
        _run_evo(
            [
                "run",
                "--root",
                str(synthetic),
                "--lane",
                "lightrag",
                "--json",
            ],
            env=env,
            allowed={6},
            error_code="QG_ACCEPTANCE_CONFLICT_NOT_CREATED",
            timeout=180,
        )
        plan = _run_evo_json(
            [
                "state",
                "replace-plan",
                "--root",
                str(synthetic),
                "--json",
            ],
            env=env,
            allowed={0},
            error_code="QG_ACCEPTANCE_REPLACE_PLAN_FAILED",
            timeout=60,
        )
        if plan.get("status") != "ready" or len(plan["plans"]) != 1:
            raise AcceptanceError(
                "QG_ACCEPTANCE_REPLACE_PLAN_NOT_READY"
            )
        reviewed = plan["plans"][0]
        first_execute_submits_before = proxy_state.count(
            "POST",
            "/documents/text",
        )
        execute_args = [
            "state",
            "replace-execute",
            "--root",
            str(synthetic),
            "--plan-id",
            reviewed["plan_id"],
            "--confirm-digest",
            reviewed["plan_digest"],
            "--smoke-query",
            "系统基准年份是什么？",
            "--json",
        ]
        delete_before = proxy_state.count(
            "DELETE",
            "/documents/delete_document",
        )
        stop_gateway()
        heartbeat = _run_evo_json(
            execute_args,
            env=env,
            allowed={6},
            error_code="QG_ACCEPTANCE_HEARTBEAT_GATE_FAILED",
            timeout=60,
        )
        if (
            heartbeat.get("error_code")
            != "QUERY_GATEWAY_HEARTBEAT_STALE"
            or proxy_state.count(
                "DELETE",
                "/documents/delete_document",
            )
            != delete_before
        ):
            raise AcceptanceError(
                "QG_ACCEPTANCE_HEARTBEAT_DID_NOT_BLOCK"
            )
        start_gateway()
        _wait_json_health(
            f"http://127.0.0.1:{gateway_port}",
            "/internal/readyz",
            timeout_seconds=60,
            error_code="QG_ACCEPTANCE_GATEWAY_UNREADY",
        )
        store = StateStore(synthetic)
        config = EvoConfig.load(synthetic)
        partition = store.query_partition(
            config.project.get("lightrag", {})
        )
        store.begin_query_run(
            request_id="acceptance-stale-lease",
            retrieval_partition_id=str(partition["id"]),
            principal_hmac="hmac-sha256:" + "1" * 64,
            query_hmac="hmac-sha256:" + "2" * 64,
            request_mode="mix",
            gateway_mode="enforce",
            verification_level="acceptance",
            lease_seconds=-1,
        )
        webhook_state.fail = True
        stale = _run_evo_json(
            execute_args,
            env=env,
            allowed={6},
            error_code="QG_ACCEPTANCE_STALE_LEASE_GATE_FAILED",
            timeout=60,
        )
        if (
            stale.get("error_code") != "QUERY_DRAIN_STALE_LEASE"
            or proxy_state.count(
                "DELETE",
                "/documents/delete_document",
            )
            != delete_before
        ):
            raise AcceptanceError(
                "QG_ACCEPTANCE_STALE_LEASE_DID_NOT_BLOCK",
                details={
                    "observed_error_code": stale.get("error_code"),
                    "delete_requests_before": delete_before,
                    "delete_requests_after": proxy_state.count(
                        "DELETE",
                        "/documents/delete_document",
                    ),
                },
            )
        _run_evo(
            [
                "gateway",
                "lease-recover",
                "--root",
                str(synthetic),
                "--request-id",
                "acceptance-stale-lease",
                "--action",
                "abandon",
                "--confirm",
                "acceptance-stale-lease",
                "--json",
            ],
            env=env,
            allowed={0},
            error_code="QG_ACCEPTANCE_LEASE_RECOVERY_FAILED",
        )
        notification_block = _run_evo_json(
            execute_args,
            env=env,
            allowed={6},
            error_code="QG_ACCEPTANCE_NOTIFICATION_GATE_FAILED",
            timeout=60,
        )
        if (
            notification_block.get("error_code")
            != "OPS_NOTIFICATION_REQUIRED_UNDELIVERED"
            or proxy_state.count(
                "DELETE",
                "/documents/delete_document",
            )
            != delete_before
        ):
            raise AcceptanceError(
                "QG_ACCEPTANCE_NOTIFICATION_DID_NOT_BLOCK"
            )
        failed = [
            item
            for item in store.list_notifications()
            if item["event_type"] == "MAINTENANCE_DRAINING"
        ][0]
        webhook_state.fail = False
        _run_evo(
            [
                "alerts",
                "retry",
                "--root",
                str(synthetic),
                "--notification-id",
                str(failed["id"]),
                "--confirm",
                str(failed["id"]),
                "--json",
            ],
            env=env,
            allowed={0},
            error_code="QG_ACCEPTANCE_NOTIFICATION_RETRY_FAILED",
        )
        _run_evo(
            [
                "alerts",
                "dispatch",
                "--root",
                str(synthetic),
                "--json",
            ],
            env=env,
            allowed={0},
            error_code="QG_ACCEPTANCE_NOTIFICATION_DISPATCH_FAILED",
        )
        completed = _run_evo_json(
            execute_args,
            env=env,
            allowed={0},
            error_code="QG_ACCEPTANCE_REPLACEMENT_FAILED",
            timeout=300,
        )
        if completed.get("status") != "completed":
            raise AcceptanceError(
                "QG_ACCEPTANCE_REPLACEMENT_NOT_COMPLETED"
            )
        first_execute_submits = (
            proxy_state.count("POST", "/documents/text")
            - first_execute_submits_before
        )

        # A second revision exercises a live in-flight reader drain.
        source.write_text(
            "QG-001验收文档再次更新：系统基准年份为2028年，"
            "维护编号为OPS-2028。",
            encoding="utf-8",
        )
        _run_evo(
            [
                "run",
                "--root",
                str(synthetic),
                "--lane",
                "lightrag",
                "--json",
            ],
            env=env,
            allowed={6},
            error_code="QG_ACCEPTANCE_SECOND_CONFLICT_FAILED",
            timeout=180,
        )
        second_plan = _run_evo_json(
            [
                "state",
                "replace-plan",
                "--root",
                str(synthetic),
                "--json",
            ],
            env=env,
            allowed={0},
            error_code="QG_ACCEPTANCE_SECOND_PLAN_FAILED",
            timeout=60,
        )["plans"][0]
        second_execute_submits_before = proxy_state.count(
            "POST",
            "/documents/text",
        )
        second_args = [
            "state",
            "replace-execute",
            "--root",
            str(synthetic),
            "--plan-id",
            second_plan["plan_id"],
            "--confirm-digest",
            second_plan["plan_digest"],
            "--smoke-query",
            "系统基准年份是什么？",
            "--json",
        ]
        proxy_state.query_delay_seconds = 4
        query_result: dict[str, Any] = {}

        def query_in_flight() -> None:
            status, payload = _http_json(
                public_url,
                "POST",
                "/api/query",
                {
                    "schema_version": 2,
                    "query": "系统基准年份是什么？",
                    "mode": "mix",
                    "top_k": 20,
                },
                basic_auth=(username, password),
                timeout=150,
            )
            query_result.update({"status": status, "payload": payload})

        query_thread = threading.Thread(
            target=query_in_flight,
            daemon=True,
        )
        query_thread.start()
        deadline = time.monotonic() + 30
        while (
            store.query_drain_status(str(partition["id"]))["active"] == 0
            and time.monotonic() < deadline
        ):
            time.sleep(0.1)
        if store.query_drain_status(str(partition["id"]))["active"] == 0:
            raise AcceptanceError(
                "QG_ACCEPTANCE_INFLIGHT_QUERY_NOT_OBSERVED"
            )
        second_completed = _run_evo_json(
            second_args,
            env=env,
            allowed={0},
            error_code="QG_ACCEPTANCE_DRAIN_REPLACEMENT_FAILED",
            timeout=300,
        )
        query_thread.join(timeout=180)
        proxy_state.query_delay_seconds = 0
        if (
            second_completed.get("status") != "completed"
            or query_result.get("status") != 503
        ):
            raise AcceptanceError("QG_ACCEPTANCE_DRAIN_FAILED")
        second_execute_submits = (
            proxy_state.count("POST", "/documents/text")
            - second_execute_submits_before
        )
        final_status, final_payload = _http_json(
            public_url,
            "POST",
            "/api/query",
            {
                "schema_version": 2,
                "query": "系统基准年份是什么？",
                "mode": "mix",
                "top_k": 20,
            },
            basic_auth=(username, password),
            timeout=150,
        )
        if (
            final_status != 200
            or final_payload.get("generation_status") != "succeeded"
            or not final_payload.get("answer")
        ):
            raise AcceptanceError(
                "QG_ACCEPTANCE_POST_REPLACEMENT_QUERY_FAILED"
            )
        maintenance_events = [
            event
            for event in webhook_state.events
            if (
                event["event_type"] == "MAINTENANCE_DRAINING"
                and event["accepted"]
            )
        ]
        delete_events = [
            item
            for item in proxy_state.calls
            if item["method"] == "DELETE"
        ]
        if (
            not maintenance_events
            or not delete_events
            or maintenance_events[0]["observed_at"]
            > delete_events[0]["observed_at"]
        ):
            raise AcceptanceError(
                "QG_ACCEPTANCE_NOTIFICATION_ORDER_INVALID"
            )
        if first_execute_submits > 1 or second_execute_submits > 1:
            raise AcceptanceError(
                "QG_ACCEPTANCE_EFFECT_ENVELOPE_EXCEEDED",
                details={
                    "first_operation_submit_requests": (
                        first_execute_submits
                    ),
                    "second_operation_submit_requests": (
                        second_execute_submits
                    ),
                },
            )
        return {
            "lease": {
                "stale_blocked_before_delete": True,
                "operator_abandon": True,
                "inflight_observed": True,
            },
            "maintenance": {
                "heartbeat_stale_blocked": True,
                "notification_failure_blocked": True,
                "notification_before_delete": True,
                "inflight_response_status": 503,
                "final_fence_state": "CLOSED",
            },
            "replacement": {
                "status": "completed",
                "operations_completed": 2,
                "delete_requests": proxy_state.count(
                    "DELETE",
                    "/documents/delete_document",
                ),
                "submit_requests": (
                    first_execute_submits + second_execute_submits
                ),
                "max_submit_requests_per_operation": max(
                    first_execute_submits,
                    second_execute_submits,
                ),
                "effect_envelope_respected": (
                    first_execute_submits <= 1
                    and second_execute_submits <= 1
                ),
                "post_replacement_query": "succeeded",
            },
            "counts": {
                "query_requests": proxy_state.count("POST", "/query"),
                "document_post_requests": (
                    proxy_state.count("POST", "/documents/text")
                ),
                "pre_replacement_document_posts": all_submits_before,
                "notification_attempts": len(webhook_state.events),
                "notification_deliveries": sum(
                    1
                    for event in webhook_state.events
                    if event["accepted"]
                ),
            },
        }


def cleanup_acceptance_run(run_id: str) -> dict[str, Any]:
    if not run_id.startswith("qg001-"):
        raise StateError(
            "acceptance cleanup run ID is invalid",
            error_code="QG_ACCEPTANCE_RUN_ID_INVALID",
        )
    container_ids = _run_output(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label={LABEL_CARD}={CARD}",
            "--filter",
            f"label={LABEL_RUN}={run_id}",
        ],
        error_code="QG_ACCEPTANCE_CLEANUP_DISCOVERY_FAILED",
    ).split()
    runtime_paths: set[Path] = {
        path.resolve()
        for path in Path(tempfile.gettempdir()).glob(
            f"evo-wiki-{run_id}-*"
        )
        if path.is_dir()
        and _is_acceptance_runtime_path(path.resolve(), run_id)
    }
    for container_id in container_ids:
        raw = _run_output(
            ["docker", "inspect", container_id],
            error_code="QG_ACCEPTANCE_CLEANUP_INSPECT_FAILED",
        )
        parsed = json.loads(raw)[0]
        for mount in parsed.get("Mounts", []):
            source = mount.get("Source")
            if isinstance(source, str):
                candidate = Path(source).resolve()
                if _is_acceptance_runtime_path(candidate, run_id):
                    runtime_paths.add(_runtime_root(candidate, run_id))
        _run_checked(
            ["docker", "rm", "-f", container_id],
            error_code="QG_ACCEPTANCE_CLEANUP_CONTAINER_FAILED",
        )
    network_ids = _run_output(
        [
            "docker",
            "network",
            "ls",
            "-q",
            "--filter",
            f"label={LABEL_CARD}={CARD}",
            "--filter",
            f"label={LABEL_RUN}={run_id}",
        ],
        error_code="QG_ACCEPTANCE_CLEANUP_DISCOVERY_FAILED",
    ).split()
    for network_id in network_ids:
        _run_checked(
            ["docker", "network", "rm", network_id],
            error_code="QG_ACCEPTANCE_CLEANUP_NETWORK_FAILED",
        )
    stopped_gateways = 0
    for path in runtime_paths:
        stopped_gateways += int(_stop_labelled_gateway(path))
        shutil.rmtree(path, ignore_errors=True)
    return {
        "schema_version": 1,
        "card": CARD,
        "run_id": run_id,
        "status": "cleaned",
        "containers_removed": len(container_ids),
        "networks_removed": len(network_ids),
        "gateway_processes_stopped": stopped_gateways,
        "runtime_directories_removed": len(runtime_paths),
        "error_code": None,
    }


def _remote_summary(config: dict[str, Any]) -> dict[str, Any]:
    service = resolve_lightrag_service_config(config)
    client = LightRAGServiceClient(
        service["base_url"],
        headers=service["headers"],
        timeout=min(float(service["timeout_seconds"]), 10),
        workspace=service["workspace"],
    )
    inventory = client.request_json(
        "POST",
        "/documents/paginated",
        {
            "page": 1,
            "page_size": 200,
            "sort_field": "file_path",
            "sort_direction": "asc",
        },
    )
    pipeline = client.request_json(
        "GET",
        "/documents/pipeline_status",
    )
    documents = inventory.get("documents") if isinstance(inventory, dict) else None
    if not isinstance(documents, list) or not isinstance(pipeline, dict):
        raise AcceptanceError("QG_ACCEPTANCE_REMOTE_INVALID")
    counts: dict[str, int] = {}
    chunks = 0
    for document in documents:
        if not isinstance(document, dict):
            raise AcceptanceError("QG_ACCEPTANCE_REMOTE_INVALID")
        status = str(document.get("status"))
        counts[status] = counts.get(status, 0) + 1
        chunk_count = document.get("chunks_count", 0)
        if isinstance(chunk_count, int) and not isinstance(
            chunk_count,
            bool,
        ):
            chunks += chunk_count
    return {
        "document_count": len(documents),
        "status_counts": counts,
        "chunks_count": chunks,
        "pipeline_idle": not bool(
            pipeline.get("busy", pipeline.get("is_busy", False))
        ),
    }


def _source_remote_is_expected(summary: dict[str, Any]) -> bool:
    remote = summary.get("remote") or {}
    return (
        summary.get("database_schema_version") == 1
        and summary.get("state_commit_seq") == 15
        and remote.get("document_count") == 9
        and remote.get("status_counts") == {"processed": 9}
        and remote.get("chunks_count") == 34
        and remote.get("pipeline_idle") is True
    )


def _workspace_manifest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        stat = path.stat()
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(str(stat.st_mode & 0o777).encode("ascii"))
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
    return "sha256:" + digest.hexdigest()


def _file_guard(path: Path) -> tuple[int, int, str]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, _sha256_file(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _command_ok(command: list[str]) -> bool:
    try:
        return (
            subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _docker_image_present(image: str) -> bool:
    return _command_ok(["docker", "image", "inspect", image])


def _docker_image_digest(image: str) -> str:
    raw = _run_output(
        [
            "docker",
            "image",
            "inspect",
            image,
            "--format",
            "{{.Id}}",
        ],
        error_code="QG_ACCEPTANCE_IMAGE_INSPECT_FAILED",
    )
    return raw.strip()


def _docker_host_port(container: str, port: int) -> int:
    raw = _run_output(
        [
            "docker",
            "port",
            container,
            f"{port}/tcp",
        ],
        error_code="QG_ACCEPTANCE_PORT_DISCOVERY_FAILED",
    )
    value = raw.strip().rsplit(":", 1)[-1]
    try:
        return int(value)
    except ValueError as exc:
        raise AcceptanceError(
            "QG_ACCEPTANCE_PORT_DISCOVERY_FAILED"
        ) from exc


def _run_checked(
    command: list[str],
    *,
    error_code: str,
    timeout: float = 60,
) -> None:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcceptanceError(error_code) from exc
    if result.returncode != 0:
        raise AcceptanceError(error_code)


def _run_output(
    command: list[str],
    *,
    error_code: str,
    timeout: float = 60,
) -> str:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcceptanceError(error_code) from exc
    if result.returncode != 0:
        raise AcceptanceError(error_code)
    return result.stdout


def _run_evo(
    args: list[str],
    *,
    env: dict[str, str],
    allowed: set[int],
    error_code: str,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "evo_wiki.cli", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcceptanceError(error_code) from exc
    if result.returncode not in allowed:
        raise AcceptanceError(error_code)
    return result


def _run_evo_json(
    args: list[str],
    *,
    env: dict[str, str],
    allowed: set[int],
    error_code: str,
    timeout: float = 120,
) -> dict[str, Any]:
    result = _run_evo(
        args,
        env=env,
        allowed=allowed,
        error_code=error_code,
        timeout=timeout,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AcceptanceError(error_code) from exc
    if not isinstance(payload, dict):
        raise AcceptanceError(error_code)
    return payload


def _start_gateway(
    root: Path,
    env: dict[str, str],
) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "evo_wiki.cli",
            "gateway",
            "serve",
            "--root",
            str(root),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    pid_path = root.parent / "gateway.pid"
    pid_path.write_text(f"{process.pid}\n", encoding="ascii")
    pid_path.chmod(0o600)
    return process


def _stop_process(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _replace_process(
    holder: dict[str, subprocess.Popen[bytes] | None],
    replacement: subprocess.Popen[bytes] | None,
) -> subprocess.Popen[bytes] | None:
    current = holder.get("process")
    if current is not replacement:
        _stop_process(current)
    holder["process"] = replacement
    return replacement


def _gateway_port(root: Path) -> int:
    config = EvoConfig.load(root)
    raw = str(config.project["query_gateway"]["listen"])
    return int(raw.rsplit(":", 1)[-1])


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_json_health(
    base_url: str,
    path: str,
    *,
    timeout_seconds: float,
    error_code: str,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            status, payload = _http_json(base_url, "GET", path)
            if status == 200 and isinstance(payload, dict):
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise AcceptanceError(error_code)


def _wait_http_status(
    base_url: str,
    path: str,
    *,
    expected_status: int,
    timeout_seconds: float,
    error_code: str,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            status, _ = _http_json(base_url, "GET", path)
            if status == expected_status:
                return
        except OSError:
            pass
        time.sleep(0.25)
    raise AcceptanceError(error_code)


def _http_json(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    basic_auth: tuple[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15,
) -> tuple[int, dict[str, Any]]:
    parsed = urlsplit(base_url)
    connection = http.client.HTTPConnection(
        parsed.hostname,
        parsed.port,
        timeout=timeout,
    )
    body = (
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if payload is not None
        else None
    )
    request_headers = dict(headers or {})
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if basic_auth is not None:
        token = base64.b64encode(
            f"{basic_auth[0]}:{basic_auth[1]}".encode("utf-8")
        ).decode("ascii")
        request_headers["Authorization"] = f"Basic {token}"
    try:
        connection.request(
            method,
            path,
            body=body,
            headers=request_headers,
        )
        response = connection.getresponse()
        raw = response.read(8 * 1024 * 1024)
        status = int(response.status)
    finally:
        connection.close()
    try:
        decoded = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        decoded = {}
    return status, decoded if isinstance(decoded, dict) else {}


def _start_gateway_relay(
    state: _GatewayRelayState,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    target = urlsplit(state.target_url)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._forward()

        def do_POST(self) -> None:  # noqa: N802
            self._forward()

        def _forward(self) -> None:
            if (
                self.headers.get("X-Evo-Acceptance-Relay")
                != state.token
            ):
                self.send_response(404)
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else None
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower()
                not in {
                    "connection",
                    "content-length",
                    "host",
                    "x-evo-acceptance-relay",
                }
            }
            connection = http.client.HTTPConnection(
                target.hostname,
                target.port,
                timeout=180,
            )
            try:
                connection.request(
                    self.command,
                    self.path,
                    body=body,
                    headers=headers,
                )
                response = connection.getresponse()
                raw = response.read(16 * 1024 * 1024)
                status = int(response.status)
                content_type = response.getheader(
                    "Content-Type",
                    "application/json",
                )
            finally:
                connection.close()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_: Any) -> None:
            return

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _start_webhook(
    state: _WebhookState,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            timestamp = self.headers.get("X-Evo-Timestamp", "")
            signature = self.headers.get("X-Evo-Signature", "")
            expected = hmac.new(
                state.signing_key,
                timestamp.encode("utf-8") + b"." + body,
                hashlib.sha256,
            ).hexdigest()
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {}
            with state.lock:
                fail = state.fail
                state.events.append(
                    {
                        "event_id": self.headers.get("X-Evo-Event-ID"),
                        "event_type": str(payload.get("event_type")),
                        "signature_valid": signature == f"v1={expected}",
                        "payload_safe": _notification_payload_safe(
                            payload
                        ),
                        "accepted": not fail,
                        "observed_at": time.monotonic(),
                    }
                )
            self.send_response(503 if fail else 204)
            self.end_headers()

        def log_message(self, *_: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _start_proxy(
    state: _ProxyState,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    target = urlsplit(state.target_url)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._forward()

        def do_POST(self) -> None:  # noqa: N802
            self._forward()

        def do_DELETE(self) -> None:  # noqa: N802
            self._forward()

        def _forward(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else None
            connection = http.client.HTTPConnection(
                target.hostname,
                target.port,
                timeout=180,
            )
            try:
                connection.request(
                    self.command,
                    self.path,
                    body=body,
                    headers={
                        "Content-Type": self.headers.get(
                            "Content-Type",
                            "application/json",
                        )
                    },
                )
                response = connection.getresponse()
                raw = response.read(16 * 1024 * 1024)
                status = int(response.status)
                content_type = response.getheader(
                    "Content-Type",
                    "application/json",
                )
            finally:
                connection.close()
            path_only = self.path.split("?", 1)[0]
            with state.lock:
                state.calls.append(
                    {
                        "method": self.command,
                        "path": path_only,
                        "observed_at": time.monotonic(),
                    }
                )
                mutate = (
                    state.mutate_reference and path_only == "/query"
                )
                delay = (
                    state.query_delay_seconds
                    if path_only == "/query"
                    else 0
                )
            if mutate and status < 300:
                try:
                    payload = json.loads(raw)
                    references = payload.get(
                        "references",
                        payload.get("ref_results", []),
                    )
                    if isinstance(references, list):
                        for reference in references:
                            if isinstance(reference, dict):
                                for key in (
                                    "file_path",
                                    "file_source",
                                    "source",
                                    "path",
                                ):
                                    if key in reference:
                                        reference[key] = (
                                            "unmapped-acceptance.md"
                                        )
                    raw = json.dumps(
                        payload,
                        ensure_ascii=False,
                    ).encode("utf-8")
                except (json.JSONDecodeError, AttributeError):
                    pass
            if delay:
                time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _cleanup_runtime(
    *,
    runtime: Path,
    containers: list[str],
    networks: list[str],
) -> dict[str, Any]:
    removed_containers = 0
    removed_networks = 0
    for container in reversed(containers):
        result = subprocess.run(
            ["docker", "rm", "-f", container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        removed_containers += int(
            result.returncode == 0
            or not _command_ok(
                ["docker", "container", "inspect", container]
            )
        )
    for network in reversed(networks):
        result = subprocess.run(
            ["docker", "network", "rm", network],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        removed_networks += int(
            result.returncode == 0
            or not _command_ok(["docker", "network", "inspect", network])
        )
    shutil.rmtree(runtime, ignore_errors=True)
    complete = (
        not runtime.exists()
        and removed_containers == len(containers)
        and removed_networks == len(networks)
    )
    return {
        "complete": complete,
        "containers_removed": removed_containers,
        "networks_removed": removed_networks,
        "runtime_removed": not runtime.exists(),
    }


def _is_acceptance_runtime_path(path: Path, run_id: str) -> bool:
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        path.relative_to(temp_root)
    except ValueError:
        return False
    return f"evo-wiki-{run_id}-" in str(path)


def _runtime_root(path: Path, run_id: str) -> Path:
    for parent in (path, *path.parents):
        if parent.name.startswith(f"evo-wiki-{run_id}-"):
            return parent
    return path


def _stop_labelled_gateway(runtime: Path) -> bool:
    pid_path = runtime / "gateway.pid"
    try:
        raw_pid = pid_path.read_text(encoding="ascii").strip()
        pid = int(raw_pid)
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        return False
    if pid <= 1:
        return False
    try:
        command = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    expected_root = str((runtime / "synthetic").resolve())
    if (
        command.returncode != 0
        or "evo_wiki.cli gateway serve" not in command.stdout
        or expected_root not in command.stdout
    ):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except OSError:
        return False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return True


def _has_retried_event_id(events: list[dict[str, Any]]) -> bool:
    counts: dict[str, int] = {}
    for event in events:
        event_id = event.get("event_id")
        if isinstance(event_id, str):
            counts[event_id] = counts.get(event_id, 0) + 1
    return any(count > 1 for count in counts.values())


def _notification_payload_safe(payload: Any) -> bool:
    forbidden = {
        "query",
        "question",
        "answer",
        "chunk",
        "chunks",
        "reference",
        "references",
        "source_path",
        "file_path",
        "username",
        "user",
        "webhook_url",
        "url",
        "credential",
        "credentials",
        "exception",
        "response",
        "response_body",
    }

    def visit(value: Any) -> bool:
        if isinstance(value, dict):
            return all(
                str(key).lower() not in forbidden and visit(child)
                for key, child in value.items()
            )
        if isinstance(value, list):
            return all(visit(child) for child in value)
        return True

    return isinstance(payload, dict) and visit(payload)
