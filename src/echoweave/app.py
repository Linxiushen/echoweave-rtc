from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import math
import secrets
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from echoweave import __version__
from echoweave.auth import (
    ReplayCache,
    SessionTokenClaims,
    TokenValidationError,
    authorize_session_claims,
    verify_session_token,
)
from echoweave.config import Settings
from echoweave.observability import HealthStatus, Observability
from echoweave.persona import PersonaConsentError, PersonaRegistry
from echoweave.pipeline import RealtimeSession
from echoweave.protocol import PacketKind, unpack_packet
from echoweave.runtime import (
    RuntimeFactory,
    RuntimeUnavailable,
    runtime_backends,
    runtime_capabilities,
)

LOGGER = logging.getLogger("echoweave")
WEB_ROOT = Path(__file__).with_name("web")
MIC_SAMPLE_RATE = 16_000
MIC_BYTES_PER_SAMPLE = 2
MIN_MIC_FRAME_MS = 10
MAX_MIC_FRAME_MS = 250
MIN_MIC_PAYLOAD_BYTES = (
    MIC_SAMPLE_RATE * MIC_BYTES_PER_SAMPLE * MIN_MIC_FRAME_MS // 1_000
)
MAX_MIC_PAYLOAD_BYTES = (
    MIC_SAMPLE_RATE * MIC_BYTES_PER_SAMPLE * MAX_MIC_FRAME_MS // 1_000
)
MIC_RATE_BURST_SECONDS = 1.0
MIC_RATE_REFILL_RATIO = 1.25
SUPPORTED_PROTOCOL_VERSIONS = (1,)
CONTROL_SCHEMA = "echoweave.control.v1"
MAX_SAFE_CLIENT_TIME_MS = 9_007_199_254_740_991
_PRIVATE_TRANSPORT_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)

MODEL_SOURCES = {
    "silero_vad_v5": "https://github.com/snakers4/silero-vad/tree/v5.1.2",
    "qwen3_asr_1_7b": "https://huggingface.co/Qwen/Qwen3-ASR-1.7B",
    "deepseek_v4_flash": "https://api-docs.deepseek.com/",
    "voxcpm2": "https://huggingface.co/openbmb/VoxCPM2",
    "nuwa_skill": "https://github.com/alchaincyf/nuwa-skill",
    "soulx_flashhead": "https://huggingface.co/Soul-AILab/SoulX-FlashHead-1_3B",
}

RETRYABLE_ERROR_CODES = {
    "asr_failed",
    "generation_failed",
    "idle_timeout",
    "latency_budget_exceeded",
    "runtime_unavailable",
    "server_overloaded",
    "session_expired",
    "start_timeout",
}
ERROR_CATEGORIES = {
    "asr_failed": "dependency",
    "authentication_failed": "authentication",
    "authorization_revoked": "authorization",
    "capability_mismatch": "protocol",
    "control_rate_exceeded": "rate_limit",
    "disclosure_not_acknowledged": "authorization",
    "generation_failed": "dependency",
    "idle_timeout": "timeout",
    "internal_gateway_error": "internal",
    "latency_budget_exceeded": "timeout",
    "mic_rate_exceeded": "rate_limit",
    "runtime_unavailable": "dependency",
    "server_overloaded": "dependency",
    "session_expired": "timeout",
    "session_start_rejected": "authorization",
    "start_timeout": "timeout",
    "unsupported_protocol_version": "protocol",
}
ALLOWED_ERROR_CATEGORIES = frozenset(
    {
        "authentication",
        "authorization",
        "dependency",
        "internal",
        "protocol",
        "rate_limit",
        "timeout",
        "validation",
    }
)
EVENT_CAPABILITIES = {
    "assistant.delta": "output.text_stream",
    "assistant.final": "output.text_stream",
    "avatar.segment": "output.avatar_events",
    "tts.browser": "output.browser_tts",
    "tts.format": "output.audio_pcm16",
}


class ClientTransportError(ConnectionError):
    """Raised when a client cannot consume the bounded outbound stream."""


@dataclass(slots=True)
class _OutboundItem:
    kind: str
    payload: dict[str, Any] | bytes
    size: int


class _OutboundPump:
    """Single-writer, bounded WebSocket sender with deterministic backpressure."""

    def __init__(
        self,
        websocket: WebSocket,
        *,
        max_messages: int,
        max_bytes: int,
        send_timeout_seconds: float,
        on_failure: Callable[[str], None] | None = None,
    ) -> None:
        self.websocket = websocket
        self.send_timeout_seconds = send_timeout_seconds
        self.max_bytes = max_bytes
        self.on_failure = on_failure
        self.queue: asyncio.Queue[_OutboundItem] = asyncio.Queue(max_messages)
        self.failed = asyncio.Event()
        self.failure: ClientTransportError | None = None
        self._queued_bytes = 0
        self._accepting = True
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="echoweave-websocket-sender",
            )

    async def send_json(self, payload: dict[str, Any]) -> None:
        encoded_size = len(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        self._enqueue(_OutboundItem("json", dict(payload), encoded_size))

    async def send_bytes(self, payload: bytes) -> None:
        self._enqueue(_OutboundItem("bytes", payload, len(payload)))

    def _enqueue(self, item: _OutboundItem) -> None:
        if not self._accepting or self.failed.is_set():
            raise self.failure or ClientTransportError("outbound transport is closed")
        if self.queue.full() or self._queued_bytes + item.size > self.max_bytes:
            error = ClientTransportError("outbound client buffer limit reached")
            self._mark_failed(error, failure_kind="slow_client", cancel_sender=True)
            raise error
        self._queued_bytes += item.size
        self.queue.put_nowait(item)

    def _mark_failed(
        self,
        error: ClientTransportError,
        *,
        failure_kind: str,
        cancel_sender: bool,
    ) -> None:
        if self.failed.is_set():
            return
        self.failure = error
        self._accepting = False
        self.failed.set()
        if self.on_failure is not None:
            try:
                self.on_failure(failure_kind)
            except Exception:
                LOGGER.exception("outbound failure observer failed")
        if (
            cancel_sender
            and self._task is not None
            and self._task is not asyncio.current_task()
        ):
            self._task.cancel()

    async def _run(self) -> None:
        try:
            while True:
                item = await self.queue.get()
                try:
                    async with asyncio.timeout(self.send_timeout_seconds):
                        if item.kind == "json":
                            await self.websocket.send_json(item.payload)
                        else:
                            await self.websocket.send_bytes(item.payload)
                except TimeoutError:
                    self._mark_failed(
                        ClientTransportError("outbound send timed out"),
                        failure_kind="send_timeout",
                        cancel_sender=False,
                    )
                    return
                except (OSError, RuntimeError, WebSocketDisconnect) as exc:
                    self._mark_failed(
                        ClientTransportError("outbound transport failed"),
                        failure_kind="disconnect",
                        cancel_sender=False,
                    )
                    LOGGER.debug("outbound transport stopped: %s", type(exc).__name__)
                    return
                except Exception as exc:
                    self._mark_failed(
                        ClientTransportError("outbound transport failed"),
                        failure_kind="disconnect",
                        cancel_sender=False,
                    )
                    LOGGER.warning(
                        "unexpected outbound transport failure: %s",
                        type(exc).__name__,
                        exc_info=exc,
                    )
                    return
                finally:
                    self._queued_bytes -= item.size
                    self.queue.task_done()
        finally:
            self._discard_queued()

    def _discard_queued(self) -> None:
        while True:
            try:
                queued = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queued_bytes -= queued.size
            self.queue.task_done()

    async def wait_failed(self) -> None:
        await self.failed.wait()

    async def flush(self, timeout_seconds: float) -> None:
        if self.failed.is_set():
            raise self.failure or ClientTransportError("outbound transport failed")
        await asyncio.wait_for(self.queue.join(), timeout=timeout_seconds)
        if self.failed.is_set():
            raise self.failure or ClientTransportError("outbound transport failed")

    async def stop(self) -> None:
        self._accepting = False
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._discard_queued()


class _ControlNegotiationError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _new_structured_id(prefix: str) -> str:
    """Generate a non-PII, time-sortable UUIDv7 identifier."""

    timestamp_ms = int(time.time() * 1_000) & ((1 << 48) - 1)
    random_a = secrets.randbits(12)
    random_b = secrets.randbits(62)
    value = (
        (timestamp_ms << 80) | (0x7 << 76) | (random_a << 64) | (0b10 << 62) | random_b
    )
    return f"{prefix}_{uuid.UUID(int=value)}"


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _is_loopback_host(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower().split("%", 1)[0]
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _host_header_name(value: str | None) -> str:
    if not value:
        return ""
    try:
        return urlsplit(f"//{value}", allow_fragments=False).hostname or ""
    except ValueError:
        return ""


def _scope_connection_hosts(scope: Scope) -> tuple[str, str, str]:
    client = scope.get("client")
    client_host = str(client[0]) if client else ""
    server = scope.get("server")
    server_host = str(server[0]) if server else ""
    host_headers = [
        value.decode("latin-1")
        for key, value in scope.get("headers", [])
        if key.lower() == b"host"
    ]
    requested_host = (
        _host_header_name(host_headers[0]) if len(host_headers) == 1 else ""
    )
    return client_host, server_host, requested_host


def _scope_connection_is_loopback(scope: Scope) -> bool:
    return all(_is_loopback_host(host) for host in _scope_connection_hosts(scope))


def _is_private_transport_host(
    value: str,
    *,
    allow_unspecified: bool = False,
) -> bool:
    normalized = value.strip().lower().split("%", 1)[0]
    if normalized == "localhost":
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    if allow_unspecified and address.is_unspecified:
        return True
    return address.is_loopback or any(
        address in network for network in _PRIVATE_TRANSPORT_NETWORKS
    )


def _scope_connection_is_private(scope: Scope) -> bool:
    client_host, server_host, requested_host = _scope_connection_hosts(scope)
    return (
        _is_private_transport_host(client_host)
        and _is_private_transport_host(server_host, allow_unspecified=True)
        and _is_private_transport_host(requested_host)
    )


def _scope_transport_is_allowed(
    scope: Scope,
    *,
    allow_insecure_private_transport: bool,
) -> bool:
    scope_type = scope.get("type")
    if scope_type not in {"http", "websocket"}:
        return True
    scheme = str(scope.get("scheme", "")).lower()
    if (scope_type == "http" and scheme == "https") or (
        scope_type == "websocket" and scheme == "wss"
    ):
        return True
    expected_cleartext = "http" if scope_type == "http" else "ws"
    if scheme != expected_cleartext:
        return False
    if _scope_connection_is_loopback(scope):
        return True
    return allow_insecure_private_transport and _scope_connection_is_private(scope)


def _tokenless_connection_is_local(websocket: WebSocket) -> bool:
    return _scope_connection_is_loopback(websocket.scope)


def _negotiate_protocol(
    control: dict[str, Any],
    server_capabilities: tuple[str, ...],
) -> tuple[int, frozenset[str], tuple[str, ...]]:
    raw_protocol = control.get("protocol", {})
    if raw_protocol is None:
        raw_protocol = {}
    if not isinstance(raw_protocol, dict):
        raise _ControlNegotiationError(
            "invalid_protocol_negotiation",
            "protocol negotiation must be a JSON object",
        )
    version = raw_protocol.get(
        "version",
        control.get("protocol_version", SUPPORTED_PROTOCOL_VERSIONS[-1]),
    )
    if isinstance(version, bool) or not isinstance(version, int):
        raise _ControlNegotiationError(
            "invalid_protocol_negotiation",
            "protocol version must be an integer",
        )
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise _ControlNegotiationError(
            "unsupported_protocol_version",
            "requested protocol version is not supported",
            details={"supported_versions": list(SUPPORTED_PROTOCOL_VERSIONS)},
        )
    raw_capabilities = raw_protocol.get(
        "capabilities",
        control.get("capabilities"),
    )
    if raw_capabilities is None:
        requested = set(server_capabilities)
    else:
        if not isinstance(raw_capabilities, list) or len(raw_capabilities) > 64:
            raise _ControlNegotiationError(
                "invalid_protocol_negotiation",
                "capabilities must be an array with at most 64 entries",
            )
        requested = set()
        for capability in raw_capabilities:
            if (
                not isinstance(capability, str)
                or not 1 <= len(capability) <= 128
                or any(
                    char not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
                    for char in capability
                )
            ):
                raise _ControlNegotiationError(
                    "invalid_protocol_negotiation",
                    "capability names must use lowercase protocol tokens",
                )
            requested.add(capability)
    available = set(server_capabilities)
    negotiated = frozenset(requested & available)
    has_input = bool(negotiated & {"input.audio_pcm16", "input.text"})
    has_output = bool(
        negotiated
        & {
            "output.audio_pcm16",
            "output.avatar_events",
            "output.browser_tts",
            "output.text_stream",
            "output.video_fragments",
        }
    )
    if not has_input or not has_output:
        raise _ControlNegotiationError(
            "capability_mismatch",
            "client and server have no usable input/output capability pair",
            details={"server_capabilities": list(server_capabilities)},
        )
    unavailable = tuple(sorted(requested - available))
    return version, negotiated, unavailable


async def _receive_or_transport_failure(
    websocket: WebSocket,
    outbound: _OutboundPump,
    failure_task: asyncio.Task[None],
    timeout_seconds: float,
) -> dict[str, Any]:
    receive_task = asyncio.create_task(
        websocket.receive(),
        name="echoweave-websocket-receive",
    )
    try:
        done, _ = await asyncio.wait(
            {receive_task, failure_task},
            timeout=max(0.001, timeout_seconds),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise TimeoutError
        if failure_task in done:
            failure_task.result()
            raise outbound.failure or ClientTransportError("outbound transport failed")
        return receive_task.result()
    finally:
        if not receive_task.done():
            receive_task.cancel()
        await asyncio.gather(receive_task, return_exceptions=True)


def _consume_background_task(task: asyncio.Future[Any]) -> None:
    """Retrieve a detached cleanup result so it cannot warn at loop shutdown."""

    if task.cancelled():
        return
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


async def _close_session_bounded(
    session: RealtimeSession,
    timeout_seconds: float,
) -> bool:
    """Bound caller latency while allowing shielded session cleanup to finish."""

    task = asyncio.create_task(
        session.close(),
        name=f"echoweave-session-close-{session.session_id}",
    )
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_consume_background_task)
        raise
    if task in done:
        task.result()
        return True
    task.cancel()
    task.add_done_callback(_consume_background_task)
    LOGGER.warning(
        "session cleanup exceeded shutdown budget session_id=%s",
        session.session_id,
    )
    return False


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    runtime_factory = RuntimeFactory(settings)
    observability = Observability()
    session_replay_cache = ReplayCache(settings.session_replay_cache_entries)
    session_cleanup_tasks: set[asyncio.Task[None]] = set()
    observability.health.register(
        "runtime",
        required=True,
        stale_after_seconds=max(60.0, settings.runtime_start_timeout_seconds * 2),
    )

    def update_runtime_health() -> dict[str, Any]:
        runtime_status = runtime_factory.readiness()
        state = str(runtime_status["state"])
        if runtime_status["ready"]:
            status = HealthStatus.HEALTHY
        elif state == "starting":
            status = HealthStatus.UNKNOWN
        else:
            status = HealthStatus.UNHEALTHY
        metadata: dict[str, bool | float | int | str | None] = {
            "state": state,
            "readiness_scope": str(runtime_status["readiness_scope"]),
            "dependency_reachability": str(runtime_status["dependency_reachability"]),
        }
        error_reference = runtime_status.get("error_reference")
        if isinstance(error_reference, str):
            metadata["error_reference"] = error_reference
        observability.health.record("runtime", status, metadata=metadata)
        return runtime_status

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await runtime_factory.prepare(settings.runtime_start_timeout_seconds)
        update_runtime_health()
        try:
            yield
        finally:
            if session_cleanup_tasks:
                _, pending = await asyncio.wait(
                    set(session_cleanup_tasks),
                    timeout=settings.websocket_shutdown_timeout_seconds,
                )
                if pending:
                    LOGGER.warning(
                        "application shutdown continues with %d session cleanup task(s)",
                        len(pending),
                    )
            await runtime_factory.aclose(
                max(
                    settings.runtime_start_timeout_seconds,
                    settings.websocket_shutdown_timeout_seconds,
                )
            )

    app = FastAPI(
        title="EchoWeave-RTC",
        version=__version__,
        description="Consent-first realtime voice and avatar agent gateway.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.runtime_factory = runtime_factory
    app.state.observability = observability
    hosted_llm_base_url = (
        settings.deepseek_base_url
        if settings.llm_backend == "deepseek"
        else settings.openai_compat_base_url
        if settings.llm_backend == "openai_compatible"
        else ""
    )
    hosted_llm_processor = (
        urlsplit(hosted_llm_base_url).hostname if hosted_llm_base_url else None
    )
    registry = PersonaRegistry(
        root=settings.persona_root,
        signing_key=settings.consent_signing_key,
        require_third_party_scope=(
            settings.llm_backend in {"deepseek", "openai_compatible"}
            or settings.tts_backend == "voxcpm_http"
            or settings.avatar_backend == "soulx_http"
        ),
        required_third_party_processor=hosted_llm_processor,
        state_path=(
            settings.consent_state_path if settings.consent_signing_key else None
        ),
    )
    active_connections: dict[str, int] = {}
    audio_rate_buckets: dict[str, tuple[float, float]] = {}
    control_rate_buckets: dict[str, tuple[float, float]] = {}
    connection_lock = asyncio.Lock()
    active_sessions = 0
    pending_sessions = 0
    observability.metrics.set_gauge("gateway.sessions.active", active_sessions)
    observability.metrics.set_gauge("gateway.sessions.pending", pending_sessions)
    server_capabilities = runtime_capabilities(settings)
    public_runtime_backends = runtime_backends(settings)

    def record_rejection(reason: str) -> None:
        observability.metrics.increment(
            "gateway.sessions.rejected",
            labels={"reason": reason},
        )

    def record_outbound_failure(reason: str) -> None:
        observability.metrics.increment(
            "gateway.transport_failures.total",
            labels={"reason": reason},
        )
        if reason == "send_timeout":
            observability.metrics.increment("gateway.send_timeouts.total")

    def metrics_request_allowed(request: Request) -> bool:
        authorization = request.headers.get("authorization", "")
        scheme, separator, credential = authorization.partition(" ")
        if settings.session_signing_key:
            if not separator or scheme.lower() != "bearer":
                return False
            try:
                claims = verify_session_token(
                    credential,
                    settings.session_signing_key,
                    audience=settings.session_token_audience,
                    max_lifetime_seconds=settings.session_token_max_ttl_seconds,
                    clock_skew_seconds=settings.session_token_clock_skew_seconds,
                    consume=False,
                )
            except (TokenValidationError, TypeError, ValueError):
                return False
            return "metrics.read" in claims.capabilities
        if settings.access_token:
            if not separator or scheme.lower() != "bearer":
                return False
            return hmac.compare_digest(
                credential,
                settings.access_token,
            )
        client_host = request.client.host if request.client else ""
        return bool(client_host) and _scope_connection_is_loopback(request.scope)

    async def consume_budget(
        buckets: dict[str, tuple[float, float]],
        client_host: str,
        amount: float,
        *,
        capacity: float,
        refill_rate: float,
    ) -> bool:
        now = time.monotonic()
        async with connection_lock:
            budget, updated_at = buckets.get(client_host, (capacity, now))
            budget = min(capacity, budget + (now - updated_at) * refill_rate)
            allowed = amount <= budget
            if allowed:
                budget -= amount
            buckets[client_host] = (budget, now)
            return allowed

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if not _scope_transport_is_allowed(
            request.scope,
            allow_insecure_private_transport=(
                settings.allow_insecure_private_transport
            ),
        ):
            return JSONResponse(
                {"detail": "HTTPS is required for non-loopback connections."},
                status_code=426,
                headers={"Cache-Control": "no-store"},
            )
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; media-src 'self' blob:; "
            "connect-src 'self'; worker-src 'self'; manifest-src 'self'; "
            "object-src 'none'; frame-src 'none'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "microphone=(self), camera=(), geolocation=()"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if request.scope.get("scheme") == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        if request.url.path.startswith("/api/health") or request.url.path in {
            "/api/metrics",
            "/api/ready",
        }:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    @app.get("/api/health/live")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "service": "EchoWeave-RTC",
                "version": __version__,
            }
        )

    @app.get("/api/ready")
    @app.get("/api/health/ready")
    async def readiness() -> JSONResponse:
        runtime_status = update_runtime_health()
        ready = bool(runtime_status["ready"])
        return JSONResponse(
            {
                "ok": ready,
                "service": "EchoWeave-RTC",
                "version": __version__,
                "status": "ready" if ready else "not_ready",
                "checks": {"runtime": runtime_status},
            },
            status_code=200 if ready else 503,
        )

    @app.get("/api/metrics")
    async def metrics(request: Request) -> JSONResponse:
        if not metrics_request_allowed(request):
            return JSONResponse(
                {"detail": "metrics access denied"},
                status_code=403,
            )
        return JSONResponse(observability.metrics.snapshot())

    @app.get("/api/model-sources")
    async def model_sources() -> JSONResponse:
        return JSONResponse(MODEL_SOURCES)

    @app.get("/docs", include_in_schema=False)
    async def api_docs() -> FileResponse:
        return FileResponse(WEB_ROOT / "api.html", media_type="text/html")

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        nonlocal active_sessions, pending_sessions
        if not _scope_transport_is_allowed(
            websocket.scope,
            allow_insecure_private_transport=(
                settings.allow_insecure_private_transport
            ),
        ):
            record_rejection("insecure_transport")
            await websocket.close(
                code=1008,
                reason="WSS is required for non-loopback connections",
            )
            return
        if (
            not settings.access_token
            and not settings.session_signing_key
            and not _tokenless_connection_is_local(websocket)
        ):
            record_rejection("access_policy")
            await websocket.close(
                code=1008,
                reason="access token required for non-loopback connections",
            )
            return
        origin = websocket.headers.get("origin")
        if not settings.origin_allowed(origin):
            record_rejection("origin")
            await websocket.close(code=1008, reason="origin rejected")
            return
        client_host = websocket.client.host if websocket.client else "unknown"
        capacity_rejected = False
        capacity_reason = "capacity"
        admission_reserved = False

        session_id = _new_structured_id("ews")
        connected_at = time.monotonic()
        session_deadline = connected_at + settings.max_session_seconds
        authorization_deadline: float | None = None
        start_deadline = connected_at + settings.session_start_timeout_seconds
        last_client_activity = connected_at
        start_attempts = 0
        sequence = 0
        accepted = False
        active_metric_registered = False
        close_code = 1000
        close_reason = "session_complete"
        session: RealtimeSession | None = None
        transport_failure_task: asyncio.Task[None] | None = None
        negotiated_capabilities: frozenset[str] | None = None
        outbound = _OutboundPump(
            websocket,
            max_messages=settings.outbound_queue_max_messages,
            max_bytes=settings.outbound_queue_max_bytes,
            send_timeout_seconds=settings.websocket_send_timeout_seconds,
            on_failure=record_outbound_failure,
        )

        def capability_enabled(capability: str) -> bool:
            return (
                negotiated_capabilities is None or capability in negotiated_capabilities
            )

        async def emit_json(payload: dict[str, Any]) -> None:
            nonlocal sequence
            required_capability = EVENT_CAPABILITIES.get(str(payload.get("type", "")))
            if required_capability and not capability_enabled(required_capability):
                return
            event = dict(payload)
            sequence += 1
            event.setdefault("session_id", session_id)
            event.setdefault("event_id", _new_structured_id("evt"))
            event.setdefault("sequence", sequence)
            event.setdefault("server_time_ms", int(time.time() * 1_000))
            if event.get("type") == "error":
                code = str(event.get("code") or "internal_gateway_error")
                event["code"] = code
                event.setdefault("error_id", _new_structured_id("err"))
                category = ERROR_CATEGORIES.get(code, event.get("category"))
                if (
                    not isinstance(category, str)
                    or category not in ALLOWED_ERROR_CATEGORIES
                ):
                    category = "validation"
                event["category"] = category
                if not isinstance(event.get("retryable"), bool):
                    event["retryable"] = code in RETRYABLE_ERROR_CODES
                if not isinstance(event.get("fatal"), bool):
                    event["fatal"] = False
                observability.metrics.increment(
                    "gateway.errors.total",
                    labels={"category": category},
                )
            await outbound.send_json(event)

        async def emit_binary(payload: bytes) -> None:
            if negotiated_capabilities is not None:
                try:
                    packet = unpack_packet(payload)
                except ValueError as exc:
                    raise RuntimeError(
                        "server generated an invalid media packet"
                    ) from exc
                if (
                    packet.kind == PacketKind.TTS_PCM16
                    and "output.audio_pcm16" not in negotiated_capabilities
                ):
                    return
                if (
                    packet.kind == PacketKind.VIDEO_FRAGMENT
                    and "output.video_fragments" not in negotiated_capabilities
                ):
                    return
            await outbound.send_bytes(payload)

        async def emit_error(
            code: str,
            message: str,
            *,
            fatal: bool = False,
            retryable: bool | None = None,
            details: dict[str, Any] | None = None,
        ) -> None:
            payload: dict[str, Any] = {
                "type": "error",
                "code": code,
                "message": message,
                "fatal": fatal,
            }
            if retryable is not None:
                payload["retryable"] = retryable
            if details:
                payload["details"] = details
            await emit_json(payload)

        async def cleanup_session_transport() -> None:
            if session is not None:
                with contextlib.suppress(asyncio.TimeoutError, Exception):
                    await _close_session_bounded(
                        session,
                        settings.websocket_shutdown_timeout_seconds,
                    )
            if accepted and not outbound.failed.is_set():
                with contextlib.suppress(asyncio.TimeoutError, Exception):
                    await outbound.flush(settings.websocket_shutdown_timeout_seconds)
                with contextlib.suppress(Exception):
                    await websocket.close(code=close_code, reason=close_reason)
            await outbound.stop()
            if transport_failure_task is not None:
                if not transport_failure_task.done():
                    transport_failure_task.cancel()
                await asyncio.gather(
                    transport_failure_task,
                    return_exceptions=True,
                )

        async def reserve_session_capacity() -> bool:
            nonlocal admission_reserved, pending_sessions
            async with connection_lock:
                if active_sessions + pending_sessions >= settings.max_active_sessions:
                    return False
                pending_sessions += 1
                admission_reserved = True
                observability.metrics.set_gauge(
                    "gateway.sessions.pending",
                    pending_sessions,
                )
                return True

        def release_pending_reservation() -> None:
            nonlocal admission_reserved, pending_sessions
            if not admission_reserved:
                return
            pending_sessions = max(0, pending_sessions - 1)
            admission_reserved = False
            observability.metrics.set_gauge(
                "gateway.sessions.pending",
                pending_sessions,
            )

        def activate_pending_reservation() -> None:
            nonlocal active_metric_registered, active_sessions
            if not admission_reserved:
                raise RuntimeError("session capacity was not reserved")
            release_pending_reservation()
            active_sessions += 1
            active_metric_registered = True
            observability.metrics.set_gauge(
                "gateway.sessions.active",
                active_sessions,
            )

        async with connection_lock:
            now = time.monotonic()
            if len(audio_rate_buckets) + len(control_rate_buckets) > 8_192:
                for buckets in (audio_rate_buckets, control_rate_buckets):
                    stale_hosts = [
                        host
                        for host, (_, updated_at) in buckets.items()
                        if host not in active_connections and now - updated_at > 120
                    ]
                    for host in stale_hosts:
                        buckets.pop(host, None)
            connection_count = active_connections.get(client_host, 0)
            if connection_count >= settings.max_connections_per_ip:
                capacity_rejected = True
                capacity_reason = "per_ip_capacity"
            else:
                active_connections[client_host] = connection_count + 1
                audio_rate_buckets.setdefault(
                    client_host,
                    (MIC_RATE_BURST_SECONDS, now),
                )
                control_rate_buckets.setdefault(
                    client_host,
                    (float(settings.control_rate_burst), now),
                )
        if capacity_rejected:
            record_rejection(capacity_reason)
            await websocket.close(code=1013, reason="connection capacity reached")
            return

        try:
            await websocket.accept()
            accepted = True
            observability.metrics.increment("gateway.sessions.total")
            outbound.start()
            transport_failure_task = asyncio.create_task(
                outbound.wait_failed(),
                name=f"echoweave-websocket-failure-watch-{session_id}",
            )
            await emit_json(
                {
                    "type": "session.hello",
                    "protocol": {
                        "name": "echoweave.media",
                        "magic": "EW",
                        "supported_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
                        "preferred_version": SUPPORTED_PROTOCOL_VERSIONS[-1],
                        "control_schema": CONTROL_SCHEMA,
                        "audio_input": {
                            "codec": "pcm_s16le",
                            "sample_rate_hz": MIC_SAMPLE_RATE,
                            "channels": 1,
                            "frame_duration_ms": {
                                "min": MIN_MIC_FRAME_MS,
                                "max": MAX_MIC_FRAME_MS,
                            },
                        },
                    },
                    "capabilities": list(server_capabilities),
                    "runtime": {"backends": public_runtime_backends},
                    "limits": {
                        "max_control_bytes": settings.max_ws_message_bytes,
                        "max_text_chars": settings.max_text_chars,
                        "max_session_seconds": settings.max_session_seconds,
                        "start_timeout_seconds": settings.session_start_timeout_seconds,
                    },
                    "requires_ai_disclosure_ack": True,
                }
            )
            while True:
                now = time.monotonic()
                if now >= session_deadline:
                    authorization_expired = (
                        authorization_deadline is not None
                        and authorization_deadline <= session_deadline
                    )
                    await emit_error(
                        "session_expired",
                        (
                            "session authorization expired"
                            if authorization_expired
                            else "session duration limit reached"
                        ),
                        fatal=True,
                    )
                    close_code, close_reason = 1000, "session_expired"
                    break
                if session is None:
                    receive_deadline = min(start_deadline, session_deadline)
                else:
                    receive_deadline = min(
                        session_deadline,
                        last_client_activity + settings.session_idle_timeout_seconds,
                    )
                if receive_deadline <= now:
                    if session is None:
                        await emit_error(
                            "start_timeout",
                            "session start deadline reached",
                            fatal=True,
                        )
                        close_code, close_reason = 1008, "start_timeout"
                    else:
                        await emit_error(
                            "idle_timeout",
                            "session was idle beyond the configured limit",
                            fatal=True,
                        )
                        close_code, close_reason = 1001, "idle_timeout"
                    break
                try:
                    message = await _receive_or_transport_failure(
                        websocket,
                        outbound,
                        transport_failure_task,
                        receive_deadline - now,
                    )
                except TimeoutError:
                    continue
                if message["type"] == "websocket.disconnect":
                    break
                last_client_activity = time.monotonic()

                if message.get("bytes") is not None:
                    if session is None:
                        await emit_error(
                            "session_not_started",
                            "send a start control message before media",
                            fatal=True,
                        )
                        close_code, close_reason = 1008, "session_not_started"
                        break
                    if not capability_enabled("input.audio_pcm16"):
                        await emit_error(
                            "unsupported_capability",
                            "audio input was not negotiated for this session",
                        )
                        continue
                    raw_packet = message["bytes"]
                    if len(raw_packet) > settings.max_ws_message_bytes:
                        await emit_error(
                            "media_packet_too_large",
                            "media packet exceeds the configured limit",
                            fatal=True,
                        )
                        close_code, close_reason = 1009, "media_packet_too_large"
                        break
                    try:
                        packet = unpack_packet(raw_packet)
                    except ValueError:
                        await emit_error(
                            "invalid_media_packet",
                            "media packet failed protocol validation",
                            fatal=True,
                        )
                        close_code, close_reason = 1008, "invalid_media_packet"
                        break
                    if packet.kind != PacketKind.MIC_PCM16:
                        await emit_error(
                            "unsupported_client_media",
                            "clients may only send microphone PCM",
                            fatal=True,
                        )
                        close_code, close_reason = 1008, "unsupported_client_media"
                        break
                    if (
                        len(packet.payload) < MIN_MIC_PAYLOAD_BYTES
                        or len(packet.payload) > MAX_MIC_PAYLOAD_BYTES
                        or len(packet.payload) % MIC_BYTES_PER_SAMPLE
                    ):
                        await emit_error(
                            "invalid_mic_frame",
                            (
                                f"microphone PCM frames must be {MIN_MIC_FRAME_MS}-"
                                f"{MAX_MIC_FRAME_MS} ms of 16 kHz PCM16"
                            ),
                            fatal=True,
                        )
                        close_code, close_reason = 1008, "invalid_mic_frame"
                        break
                    duration_seconds = len(packet.payload) / (
                        MIC_SAMPLE_RATE * MIC_BYTES_PER_SAMPLE
                    )
                    if not await consume_budget(
                        audio_rate_buckets,
                        client_host,
                        duration_seconds,
                        capacity=MIC_RATE_BURST_SECONDS,
                        refill_rate=MIC_RATE_REFILL_RATIO,
                    ):
                        await emit_error(
                            "mic_rate_exceeded",
                            "microphone audio exceeded realtime rate",
                            fatal=True,
                        )
                        close_code, close_reason = 1008, "mic_rate_exceeded"
                        break
                    await session.ingest_pcm(packet.payload, MIC_SAMPLE_RATE)
                    continue

                raw_text = message.get("text")
                if raw_text is None:
                    continue
                if not await consume_budget(
                    control_rate_buckets,
                    client_host,
                    1.0,
                    capacity=float(settings.control_rate_burst),
                    refill_rate=settings.control_rate_per_second,
                ):
                    record_rejection("rate_limit")
                    await emit_error(
                        "control_rate_exceeded",
                        "control message rate limit reached",
                        fatal=True,
                    )
                    close_code, close_reason = 1008, "control_rate_exceeded"
                    break
                if len(raw_text.encode("utf-8")) > settings.max_ws_message_bytes:
                    await emit_error(
                        "control_message_too_large",
                        "control message exceeds the configured limit",
                        fatal=True,
                    )
                    close_code, close_reason = 1009, "control_message_too_large"
                    break
                try:
                    control = json.loads(
                        raw_text,
                        parse_constant=_reject_nonfinite_json_constant,
                    )
                except (json.JSONDecodeError, RecursionError, ValueError):
                    await emit_error(
                        "invalid_control_json",
                        "control messages must be valid JSON objects",
                    )
                    continue
                if not isinstance(control, dict):
                    await emit_error(
                        "invalid_control_json",
                        "control messages must be JSON objects",
                    )
                    continue
                action = control.get("type")
                if not isinstance(action, str) or not action:
                    await emit_error(
                        "invalid_control_message",
                        "control message type must be a non-empty string",
                    )
                    continue

                if action == "ping":
                    response: dict[str, Any] = {"type": "session.pong"}
                    client_time = control.get("client_time_ms")
                    if (
                        isinstance(client_time, (int, float))
                        and not isinstance(client_time, bool)
                        and abs(client_time) <= MAX_SAFE_CLIENT_TIME_MS
                        and math.isfinite(client_time)
                    ):
                        response["client_time_ms"] = client_time
                    await emit_json(response)
                    continue
                if action == "stop":
                    close_code, close_reason = 1000, "client_stop"
                    break
                if action == "start":
                    if session is not None:
                        await emit_error(
                            "session_already_started",
                            "open a new connection to change session negotiation",
                        )
                        continue
                    start_attempts += 1
                    if start_attempts > 3:
                        record_rejection("start_attempts")
                        await emit_error(
                            "start_attempt_limit",
                            "session start attempt limit reached",
                            fatal=True,
                        )
                        close_code, close_reason = 1008, "start_attempt_limit"
                        break
                    try:
                        (
                            protocol_version,
                            candidate_capabilities,
                            unavailable_capabilities,
                        ) = _negotiate_protocol(control, server_capabilities)
                    except _ControlNegotiationError as exc:
                        record_rejection("negotiation")
                        await emit_error(
                            exc.code,
                            str(exc),
                            details=exc.details,
                        )
                        continue
                    raw_token = control.get("access_token", "")
                    supplied_token = raw_token if isinstance(raw_token, str) else ""
                    session_claims: SessionTokenClaims | None = None
                    if settings.session_signing_key:
                        try:
                            session_claims = verify_session_token(
                                supplied_token,
                                settings.session_signing_key,
                                audience=settings.session_token_audience,
                                max_lifetime_seconds=(
                                    settings.session_token_max_ttl_seconds
                                ),
                                clock_skew_seconds=(
                                    settings.session_token_clock_skew_seconds
                                ),
                                consume=False,
                            )
                        except (TokenValidationError, TypeError, ValueError):
                            record_rejection("authentication")
                            await emit_error(
                                "authentication_failed",
                                "session token rejected",
                            )
                            continue
                    elif settings.access_token and not hmac.compare_digest(
                        supplied_token,
                        settings.access_token,
                    ):
                        record_rejection("authentication")
                        await emit_error(
                            "authentication_failed",
                            "session token rejected",
                        )
                        continue
                    if control.get("ai_disclosure_ack") is not True:
                        record_rejection("disclosure")
                        await emit_error(
                            "disclosure_not_acknowledged",
                            "AI identity disclosure must be acknowledged",
                        )
                        continue
                    try:
                        raw_persona_id = control.get("persona_id", "demo")
                        if not isinstance(raw_persona_id, str):
                            raise PersonaConsentError("persona ID must be a string")
                        if session_claims is not None:
                            authorize_session_claims(
                                session_claims,
                                persona_id=raw_persona_id,
                                capabilities=candidate_capabilities,
                            )
                        if raw_persona_id not in settings.allowed_personas:
                            raise PersonaConsentError(
                                "persona is not allowed by this deployment"
                            )
                    except (
                        PersonaConsentError,
                        TokenValidationError,
                        ValueError,
                    ) as exc:
                        record_rejection("persona_authorization")
                        reference = f"auth_{uuid.uuid4().hex[:12]}"
                        LOGGER.warning(
                            "session start rejected session_id=%s reference=%s type=%s",
                            session_id,
                            reference,
                            type(exc).__name__,
                        )
                        await emit_error(
                            "session_start_rejected",
                            f"persona authorization rejected ({reference})",
                        )
                        continue
                    if session_claims is not None:
                        try:
                            session_claims = verify_session_token(
                                supplied_token,
                                settings.session_signing_key,
                                audience=settings.session_token_audience,
                                max_lifetime_seconds=(
                                    settings.session_token_max_ttl_seconds
                                ),
                                clock_skew_seconds=(
                                    settings.session_token_clock_skew_seconds
                                ),
                                replay_cache=session_replay_cache,
                                consume=True,
                            )
                            authorize_session_claims(
                                session_claims,
                                persona_id=raw_persona_id,
                                capabilities=candidate_capabilities,
                            )
                        except (TokenValidationError, TypeError, ValueError):
                            record_rejection("authentication")
                            await emit_error(
                                "authentication_failed",
                                "session token rejected",
                            )
                            continue
                        authorization_deadline = time.monotonic() + max(
                            0.0,
                            session_claims.expires_at - time.time(),
                        )
                        session_deadline = min(
                            session_deadline,
                            authorization_deadline,
                        )
                    if not await reserve_session_capacity():
                        record_rejection("global_capacity")
                        message = "server session capacity is exhausted"
                        if session_claims is not None:
                            message += "; request a new session token before retrying"
                        await emit_error(
                            "server_overloaded",
                            message,
                            fatal=True,
                            retryable=True,
                        )
                        close_code, close_reason = 1013, "server_overloaded"
                        break
                    try:
                        persona = registry.load(raw_persona_id)
                    except (
                        PersonaConsentError,
                        TokenValidationError,
                        ValueError,
                    ) as exc:
                        release_pending_reservation()
                        record_rejection("persona_authorization")
                        reference = f"auth_{uuid.uuid4().hex[:12]}"
                        LOGGER.warning(
                            "session start rejected session_id=%s reference=%s type=%s",
                            session_id,
                            reference,
                            type(exc).__name__,
                        )
                        await emit_error(
                            "session_start_rejected",
                            f"persona authorization rejected ({reference})",
                        )
                        continue
                    remaining_start_time = start_deadline - time.monotonic()
                    if remaining_start_time <= 0:
                        release_pending_reservation()
                        await emit_error(
                            "start_timeout",
                            "session start deadline reached",
                            fatal=True,
                        )
                        close_code, close_reason = 1008, "start_timeout"
                        break
                    try:
                        adapters = await runtime_factory.acquire(
                            min(
                                settings.runtime_start_timeout_seconds,
                                remaining_start_time,
                            )
                        )
                    except RuntimeUnavailable as exc:
                        release_pending_reservation()
                        update_runtime_health()
                        record_rejection("runtime")
                        message = f"model runtime is not ready ({exc.reference})"
                        if session_claims is not None:
                            message += "; request a new session token before retrying"
                        await emit_error(
                            "runtime_unavailable",
                            message,
                            retryable=True,
                        )
                        continue
                    try:
                        candidate_session = RealtimeSession(
                            session_id,
                            persona,
                            adapters,
                            emit_json,
                            emit_binary,
                            authorization_check=persona.revalidate,
                            max_utterance_seconds=settings.max_utterance_seconds,
                            max_text_chars=settings.max_text_chars,
                            observability=observability,
                        )
                    except Exception:
                        with contextlib.suppress(Exception):
                            await runtime_factory.release(
                                adapters,
                                settings.websocket_shutdown_timeout_seconds,
                            )
                        raise
                    negotiated_capabilities = candidate_capabilities
                    try:
                        await emit_json(
                            {
                                "type": "session.negotiated",
                                "protocol_version": protocol_version,
                                "capabilities": sorted(candidate_capabilities),
                                "unavailable_capabilities": list(
                                    unavailable_capabilities
                                ),
                            }
                        )
                        remaining_start_time = start_deadline - time.monotonic()
                        if remaining_start_time <= 0:
                            raise TimeoutError
                        await asyncio.wait_for(
                            candidate_session.start(),
                            timeout=remaining_start_time,
                        )
                    except TimeoutError:
                        with contextlib.suppress(Exception):
                            await _close_session_bounded(
                                candidate_session,
                                settings.websocket_shutdown_timeout_seconds,
                            )
                        release_pending_reservation()
                        await emit_error(
                            "start_timeout",
                            "session start deadline reached",
                            fatal=True,
                        )
                        close_code, close_reason = 1008, "start_timeout"
                        break
                    except asyncio.CancelledError:
                        # Hand ownership to the cancellation-safe finalizer.  A
                        # direct await here can be interrupted repeatedly by an
                        # ASGI cancel scope before the close coroutine even starts.
                        session = candidate_session
                        raise
                    except Exception:
                        with contextlib.suppress(Exception):
                            await _close_session_bounded(
                                candidate_session,
                                settings.websocket_shutdown_timeout_seconds,
                            )
                        raise
                    session = candidate_session
                    activate_pending_reservation()
                    observability.metrics.increment("gateway.sessions.started")
                    continue

                if session is None:
                    await emit_error(
                        "session_not_started",
                        "send a start control message first",
                    )
                    continue
                if action == "text":
                    if not capability_enabled("input.text"):
                        await emit_error(
                            "unsupported_capability",
                            "text input was not negotiated for this session",
                        )
                        continue
                    text = control.get("text", "")
                    if not isinstance(text, str):
                        await emit_error(
                            "invalid_text",
                            "text turn must contain a string",
                        )
                    elif len(text) > settings.max_text_chars:
                        await emit_error(
                            "text_too_long",
                            "text turn exceeds the configured limit",
                        )
                    else:
                        await session.submit_text(text)
                elif action == "cancel":
                    if not capability_enabled("control.cancel"):
                        await emit_error(
                            "unsupported_capability",
                            "response cancellation was not negotiated",
                        )
                    else:
                        await session.cancel_response("client_cancelled")
                else:
                    await emit_error(
                        "unknown_control_type",
                        "control message type is not supported",
                    )
        except WebSocketDisconnect:
            pass
        except ClientTransportError:
            close_code, close_reason = 1001, "client_unavailable"
            LOGGER.info("client transport ended session_id=%s", session_id)
        except asyncio.CancelledError:
            close_code, close_reason = 1012, "server_shutdown"
            raise
        except Exception:
            reference = f"gw_{uuid.uuid4().hex[:12]}"
            close_code, close_reason = 1011, "internal_gateway_error"
            LOGGER.exception(
                "websocket session failed session_id=%s reference=%s",
                session_id,
                reference,
            )
            if accepted and not outbound.failed.is_set():
                with contextlib.suppress(Exception):
                    await emit_error(
                        "internal_gateway_error",
                        f"gateway failed safely ({reference})",
                        fatal=True,
                    )
        finally:
            # Release the lightweight connection slot before any cancellation
            # checkpoint. Keep an admitted session counted until its bounded model
            # cleanup task actually finishes so admission cannot briefly exceed
            # the configured runtime capacity.
            remaining_connections = active_connections.get(client_host, 1) - 1
            if remaining_connections > 0:
                active_connections[client_host] = remaining_connections
            else:
                active_connections.pop(client_host, None)
            if not active_metric_registered:
                release_pending_reservation()
            observability.metrics.set_gauge(
                "gateway.sessions.active",
                active_sessions,
            )
            observability.metrics.set_gauge(
                "gateway.sessions.pending",
                pending_sessions,
            )
            cleanup_task = asyncio.create_task(
                cleanup_session_transport(),
                name=f"echoweave-transport-cleanup-{session_id}",
            )
            session_cleanup_tasks.add(cleanup_task)

            def cleanup_done(task: asyncio.Task[None]) -> None:
                nonlocal active_sessions
                session_cleanup_tasks.discard(task)
                _consume_background_task(task)
                if active_metric_registered:
                    active_sessions = max(0, active_sessions - 1)
                    observability.metrics.set_gauge(
                        "gateway.sessions.active",
                        active_sessions,
                    )

            cleanup_task.add_done_callback(cleanup_done)
            await asyncio.shield(cleanup_task)

    app.mount("/", StaticFiles(directory=WEB_ROOT, html=True), name="web")
    return app


app = create_app()
