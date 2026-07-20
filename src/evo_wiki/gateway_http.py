"""Optional Starlette/Uvicorn delivery layer for the trusted query gateway."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .state.notifications import NotificationDispatcher
from .query_gateway import GatewayQueryRequest, TrustedQueryGateway
from .state.contracts import StateError
from .version import __version__


def _require_gateway_dependencies() -> tuple[Any, ...]:
    try:
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Mount, Route
        from starlette.staticfiles import StaticFiles
    except ImportError as exc:
        raise StateError(
            "gateway runtime dependencies are not installed; install "
            "evo-wiki[gateway]",
            error_code="QUERY_GATEWAY_DEPENDENCY_MISSING",
        ) from exc
    return Starlette, Request, JSONResponse, Route, Mount, StaticFiles


def create_gateway_app(
    gateway: TrustedQueryGateway,
    *,
    platform_dir: Path | None = None,
) -> Any:
    (
        Starlette,
        Request,
        JSONResponse,
        Route,
        Mount,
        StaticFiles,
    ) = _require_gateway_dependencies()
    settings = gateway.settings
    semaphore = asyncio.Semaphore(settings.max_in_flight)
    instance_id = f"gateway-{uuid.uuid4().hex}"
    heartbeat_task: asyncio.Task[None] | None = None
    notification_task: asyncio.Task[None] | None = None

    def principal(request: Any) -> str | None:
        if settings.auth_mode == "local_single_user":
            return "local-single-user"
        value = request.headers.get(settings.principal_header)
        return value.strip() if isinstance(value, str) and value.strip() else None

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await asyncio.to_thread(
                    gateway.store.heartbeat_gateway_instance,
                    instance_id,
                )
            except Exception:
                # Readiness/status exposes the stale heartbeat.  The service
                # does not fabricate a healthy bookkeeping write.
                return

    async def dispatch_notifications() -> None:
        dispatcher = NotificationDispatcher(
            gateway.store,
            gateway.notification_settings,
            worker_id=instance_id,
        )
        while True:
            try:
                await asyncio.to_thread(
                    dispatcher.dispatch_due,
                    limit=20,
                )
            except Exception:
                # Durable outbox state remains visible to alerts status and
                # can be resumed by alerts dispatch or another gateway.
                return
            await asyncio.sleep(
                gateway.notification_settings.dispatch_interval_seconds
            )

    @asynccontextmanager
    async def lifespan(_: Any):
        nonlocal heartbeat_task, notification_task
        gateway.check()
        gateway.store.register_gateway_instance(
            instance_id=instance_id,
            retrieval_partition_id=str(gateway.partition["id"]),
            gateway_mode=settings.mode,
            version=__version__,
        )
        heartbeat_task = asyncio.create_task(heartbeat())
        if gateway.notification_settings.enabled:
            notification_task = asyncio.create_task(
                dispatch_notifications()
            )
        try:
            yield
        finally:
            if notification_task is not None:
                notification_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await notification_task
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            await asyncio.to_thread(
                gateway.store.stop_gateway_instance,
                instance_id,
            )

    async def health(_: Any) -> Any:
        return JSONResponse(
            {
                "status": "healthy",
                "schema_version": 1,
                "service": "evo-wiki-query-gateway",
            }
        )

    async def ready(_: Any) -> Any:
        try:
            result = await asyncio.to_thread(gateway.check)
            return JSONResponse(result)
        except StateError as exc:
            return JSONResponse(
                {
                    "status": "failed",
                    "error_code": exc.error_code,
                },
                status_code=503,
            )

    async def private_not_found(_: Any) -> Any:
        return JSONResponse(
            {
                "status": "not_found",
                "error_code": "PLATFORM_PRIVATE_PATH",
            },
            status_code=404,
        )

    async def query(request: Any) -> Any:
        identity = principal(request)
        if identity is None:
            return JSONResponse(
                {
                    "schema_version": 2,
                    "generation_status": "failed",
                    "error_code": "QUERY_AUTH_REQUIRED",
                },
                status_code=401,
            )
        content_length = request.headers.get("content-length")
        if (
            content_length is not None
            and content_length.isdigit()
            and int(content_length) > settings.max_body_bytes
        ):
            return JSONResponse(
                {
                    "schema_version": 2,
                    "generation_status": "failed",
                    "error_code": "QUERY_BODY_TOO_LARGE",
                },
                status_code=413,
            )
        body = await request.body()
        if len(body) > settings.max_body_bytes:
            return JSONResponse(
                {
                    "schema_version": 2,
                    "generation_status": "failed",
                    "error_code": "QUERY_BODY_TOO_LARGE",
                },
                status_code=413,
            )
        try:
            parsed = GatewayQueryRequest.model_validate_json(body)
        except Exception:
            return JSONResponse(
                {
                    "schema_version": 2,
                    "generation_status": "failed",
                    "error_code": "QUERY_REQUEST_INVALID",
                },
                status_code=400,
            )
        if semaphore.locked():
            return JSONResponse(
                {
                    "schema_version": 2,
                    "generation_status": "failed",
                    "error_code": "QUERY_CAPACITY_EXCEEDED",
                },
                status_code=429,
            )
        async with semaphore:
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        gateway.query,
                        parsed,
                        principal=identity,
                    ),
                    timeout=settings.request_timeout_seconds + 5,
                )
            except asyncio.TimeoutError:
                return JSONResponse(
                    {
                        "schema_version": 2,
                        "generation_status": "failed",
                        "error_code": "QUERY_GATEWAY_TIMEOUT",
                    },
                    status_code=504,
                )
            except StateError as exc:
                return JSONResponse(
                    {
                        "schema_version": 2,
                        "generation_status": "failed",
                        "error_code": exc.error_code,
                    },
                    status_code=_state_http_status(exc.error_code),
                )
        payload = result.model_dump(mode="json")
        status_code = 200
        if result.generation_status == "failed":
            status_code = (
                503
                if result.error_code == "QUERY_MAINTENANCE_ACTIVE"
                else 502
            )
        return JSONResponse(payload, status_code=status_code)

    async def graph_proxy(request: Any) -> Any:
        identity = principal(request)
        if identity is None:
            return JSONResponse(
                {"status": "failed", "error_code": "QUERY_AUTH_REQUIRED"},
                status_code=401,
            )
        if semaphore.locked():
            return JSONResponse(
                {
                    "status": "failed",
                    "error_code": "QUERY_CAPACITY_EXCEEDED",
                },
                status_code=429,
            )
        suffix = request.path_params.get("label_path")
        upstream = (
            f"/graph/label/{suffix}"
            if suffix is not None
            else "/graphs"
        )
        query_string = urlencode(list(request.query_params.multi_items()))
        if query_string:
            upstream = f"{upstream}?{query_string}"
        lease_id: str | None = None
        try:
            lease_id = await asyncio.to_thread(
                gateway.begin_reader_lease,
                principal=identity,
                request_fingerprint=upstream,
            )
            async with semaphore:
                payload = await asyncio.wait_for(
                    asyncio.to_thread(
                        gateway.client.request_json,
                        "GET",
                        upstream,
                    ),
                    timeout=settings.request_timeout_seconds + 5,
                )
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > settings.max_response_bytes:
                raise StateError(
                    "graph response exceeds the configured limit",
                    error_code="QUERY_BACKEND_RESPONSE_TOO_LARGE",
                )
            persisted = await asyncio.to_thread(
                gateway.finish_reader_lease,
                lease_id,
                success=True,
            )
            if persisted["status"] != "ANSWERED":
                return JSONResponse(
                    {
                        "status": "maintenance",
                        "error_code": "QUERY_MAINTENANCE_ACTIVE",
                    },
                    status_code=503,
                )
        except asyncio.TimeoutError:
            if lease_id is not None:
                await _finish_failed_reader(
                    gateway,
                    lease_id,
                    "QUERY_GATEWAY_TIMEOUT",
                )
            return JSONResponse(
                {
                    "status": "failed",
                    "error_code": "QUERY_GATEWAY_TIMEOUT",
                },
                status_code=504,
            )
        except StateError as exc:
            if lease_id is not None:
                await _finish_failed_reader(
                    gateway,
                    lease_id,
                    exc.error_code,
                )
            return JSONResponse(
                {"status": "failed", "error_code": exc.error_code},
                status_code=_state_http_status(exc.error_code),
            )
        except Exception:
            if lease_id is not None:
                await _finish_failed_reader(
                    gateway,
                    lease_id,
                    "QUERY_BACKEND_REQUEST_FAILED",
                )
            return JSONResponse(
                {
                    "status": "failed",
                    "error_code": "QUERY_BACKEND_REQUEST_FAILED",
                },
                status_code=502,
            )
        return JSONResponse(payload)

    routes = [
        Route("/internal/healthz", health, methods=["GET"]),
        Route("/internal/readyz", ready, methods=["GET"]),
        Route("/v1/query", query, methods=["POST"]),
        Route("/api/query", query, methods=["POST"]),
        Route("/v1/graphs", graph_proxy, methods=["GET"]),
        Route("/api/graphs", graph_proxy, methods=["GET"]),
        Route(
            "/v1/graph/label/{label_path:path}",
            graph_proxy,
            methods=["GET"],
        ),
        Route(
            "/api/graph/label/{label_path:path}",
            graph_proxy,
            methods=["GET"],
        ),
    ]
    if platform_dir is not None:
        resolved_platform = platform_dir.resolve()
        if not (resolved_platform / "index.html").is_file():
            raise StateError(
                "generated platform is missing; run evo-wiki generate first",
                error_code="PLATFORM_NOT_GENERATED",
            )
        routes.extend(
            [
                Route(
                    "/nginx.conf",
                    private_not_found,
                    methods=["GET", "HEAD"],
                ),
                Route(
                    "/README.md",
                    private_not_found,
                    methods=["GET", "HEAD"],
                ),
                Route(
                    "/status",
                    private_not_found,
                    methods=["GET", "HEAD"],
                ),
                Route(
                    "/status/{private_path:path}",
                    private_not_found,
                    methods=["GET", "HEAD"],
                ),
                Route(
                    "/project.json",
                    private_not_found,
                    methods=["GET", "HEAD"],
                ),
                Route(
                    "/wiki.json",
                    private_not_found,
                    methods=["GET", "HEAD"],
                ),
                Mount(
                    "/",
                    app=StaticFiles(
                        directory=resolved_platform,
                        html=True,
                    ),
                    name="platform",
                ),
            ]
        )
    return Starlette(routes=routes, lifespan=lifespan)


async def _finish_failed_reader(
    gateway: TrustedQueryGateway,
    lease_id: str,
    error_code: str,
) -> None:
    with contextlib.suppress(StateError):
        await asyncio.to_thread(
            gateway.finish_reader_lease,
            lease_id,
            success=False,
            error_code=error_code,
        )


def serve_gateway(gateway: TrustedQueryGateway) -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise StateError(
            "gateway runtime dependencies are not installed; install "
            "evo-wiki[gateway]",
            error_code="QUERY_GATEWAY_DEPENDENCY_MISSING",
        ) from exc
    uvicorn.run(
        create_gateway_app(gateway),
        host=gateway.settings.listen_host,
        port=gateway.settings.listen_port,
        access_log=False,
        server_header=False,
    )


def serve_platform(
    gateway: TrustedQueryGateway,
    platform_dir: Path,
    *,
    listen: str,
) -> None:
    """Serve the generated platform and governed APIs on one local port."""
    if gateway.settings.auth_mode != "local_single_user":
        raise StateError(
            "local platform preview requires local_single_user",
            error_code="PLATFORM_SERVE_AUTH_UNSAFE",
        )
    if not isinstance(listen, str) or ":" not in listen:
        raise StateError(
            "serve listen address must be host:port",
            error_code="PLATFORM_SERVE_CONFIG_INVALID",
        )
    host, raw_port = listen.rsplit(":", 1)
    try:
        port = int(raw_port)
        address = ipaddress.ip_address(host)
    except (ValueError, TypeError) as exc:
        raise StateError(
            "serve listen address is invalid",
            error_code="PLATFORM_SERVE_CONFIG_INVALID",
        ) from exc
    if not address.is_loopback or not 1 <= port <= 65535:
        raise StateError(
            "local platform preview must listen on loopback",
            error_code="PLATFORM_SERVE_BIND_UNSAFE",
        )
    try:
        import uvicorn
    except ImportError as exc:
        raise StateError(
            "platform runtime dependencies are not installed",
            error_code="QUERY_GATEWAY_DEPENDENCY_MISSING",
        ) from exc
    uvicorn.run(
        create_gateway_app(
            gateway,
            platform_dir=platform_dir,
        ),
        host=host,
        port=port,
        access_log=False,
        server_header=False,
    )


def _state_http_status(error_code: str) -> int:
    if error_code in {"QUERY_AUTH_REQUIRED", "QUERY_DOMAIN_MISMATCH"}:
        return 403
    if error_code == "QUERY_MAINTENANCE_ACTIVE":
        return 503
    if error_code.startswith("QUERY_GATEWAY_CONFIG"):
        return 503
    return 502
