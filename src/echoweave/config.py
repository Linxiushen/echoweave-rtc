from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

BACKEND_CHOICES = {
    "vad": {"energy", "silero_v5"},
    "asr": {"demo", "qwen_local", "qwen_http"},
    "llm": {"demo", "deepseek", "openai_compatible"},
    "tts": {"browser", "voxcpm_local", "voxcpm_http"},
    "avatar": {"client_lipsync", "soulx_http"},
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _tuple_env(name: str, default: str) -> tuple[str, ...]:
    return tuple(
        item.strip() for item in os.getenv(name, default).split(",") if item.strip()
    )


def _strict_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"{name} must be exactly 'true' or 'false'")


def _optional_path_env(name: str) -> Path | None:
    value = _env(name)
    return Path(value).resolve() if value else None


def _normalized_trusted_proxy_ips(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(values) > 64:
        raise ValueError("ECHOWEAVE_TRUSTED_PROXY_IPS has too many entries")
    normalized: list[str] = []
    for value in values:
        try:
            if "/" in value:
                network = ipaddress.ip_network(value, strict=True)
                if (
                    network.prefixlen == 0
                    or network.network_address.is_unspecified
                    or network.network_address.is_multicast
                ):
                    raise ValueError
                canonical = str(network)
            else:
                address = ipaddress.ip_address(value)
                if address.is_unspecified or address.is_multicast:
                    raise ValueError
                canonical = str(address)
        except ValueError as exc:
            raise ValueError(f"invalid trusted proxy IP or CIDR: {value!r}") from exc
        if canonical in normalized:
            raise ValueError(f"duplicate trusted proxy IP or CIDR: {value!r}")
        normalized.append(canonical)
    return tuple(normalized)


def _validated_tls_pair(
    certfile: Path | None,
    keyfile: Path | None,
) -> tuple[Path | None, Path | None]:
    if (certfile is None) != (keyfile is None):
        raise ValueError("TLS certificate and key files must be configured together")
    if certfile is None or keyfile is None:
        return None, None
    if not isinstance(certfile, Path) or not isinstance(keyfile, Path):
        raise TypeError("TLS certificate and key files must be filesystem paths")
    if not certfile.is_file():
        raise ValueError(f"TLS certificate file does not exist: {certfile}")
    if not keyfile.is_file():
        raise ValueError(f"TLS key file does not exist: {keyfile}")
    return certfile, keyfile


def _bind_host_is_loopback(host: str) -> bool:
    normalized = host.strip().lower().split("%", 1)[0]
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validate_remote_https_base_url(value: str, name: str) -> None:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{name} must be an HTTPS base URL without credentials, query, or fragment"
        )


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    log_level: str = "INFO"
    persona_root: Path = Path("personas")
    consent_signing_key: str = ""
    consent_state_path: Path | None = None
    access_token: str = ""
    session_signing_key: str = ""
    session_token_audience: str = "echoweave-rtc"
    session_token_max_ttl_seconds: int = 300
    session_token_clock_skew_seconds: int = 5
    session_replay_cache_entries: int = 65_536
    allowed_origins: tuple[str, ...] = ()
    trusted_proxy_ips: tuple[str, ...] = ()
    allow_insecure_private_transport: bool = False
    tls_certfile: Path | None = None
    tls_keyfile: Path | None = None
    allowed_personas: tuple[str, ...] = ("demo",)
    max_connections_per_ip: int = 4
    max_active_sessions: int = 32
    max_ws_message_bytes: int = 262_144
    max_text_chars: int = 1_000
    max_utterance_seconds: int = 60
    max_session_seconds: int = 1_800
    session_start_timeout_seconds: float = 15.0
    runtime_start_timeout_seconds: float = 30.0
    session_idle_timeout_seconds: float = 300.0
    websocket_send_timeout_seconds: float = 5.0
    websocket_shutdown_timeout_seconds: float = 5.0
    outbound_queue_max_messages: int = 128
    outbound_queue_max_bytes: int = 64 * 1024 * 1024
    control_rate_per_second: float = 10.0
    control_rate_burst: int = 20

    vad_backend: str = "energy"
    asr_backend: str = "demo"
    llm_backend: str = "demo"
    tts_backend: str = "browser"
    avatar_backend: str = "client_lipsync"

    qwen_model: str = "Qwen/Qwen3-ASR-1.7B"
    qwen_base_url: str = "http://127.0.0.1:8001/v1"
    qwen_api_key: str = "EMPTY"

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_thinking: str = "disabled"

    openai_compat_api_key: str = ""
    openai_compat_base_url: str = ""
    openai_compat_model: str = ""
    openai_compat_max_tokens: int = 800

    voxcpm_model: str = "openbmb/VoxCPM2"
    voxcpm_base_url: str = "http://127.0.0.1:8002/v1"
    voxcpm_api_key: str = "EMPTY"
    voxcpm_sample_rate: int = 48_000
    voxcpm_worker_token: str = ""
    voxcpm_worker_audience: str = "echoweave-voxcpm-worker"
    soulx_worker_token: str = ""
    soulx_worker_audience: str = "echoweave-soulx-worker"
    worker_assertion_ttl_seconds: int = 120
    model_worker_token: str = ""

    soulx_base_url: str = "http://127.0.0.1:8003"

    @classmethod
    def from_env(cls) -> Settings:
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass
        return cls(
            host=_env("ECHOWEAVE_HOST", "127.0.0.1"),
            port=int(_env("ECHOWEAVE_PORT", "8765")),
            log_level=_env("ECHOWEAVE_LOG_LEVEL", "INFO").upper(),
            persona_root=Path(_env("ECHOWEAVE_PERSONA_ROOT", "personas")).resolve(),
            consent_signing_key=_env("ECHOWEAVE_CONSENT_SIGNING_KEY"),
            consent_state_path=Path(
                _env(
                    "ECHOWEAVE_CONSENT_STATE_PATH",
                    "runtime/consent-state.json",
                )
            ).resolve(),
            access_token=_env("ECHOWEAVE_ACCESS_TOKEN"),
            session_signing_key=_env("ECHOWEAVE_SESSION_SIGNING_KEY"),
            session_token_audience=_env(
                "ECHOWEAVE_SESSION_TOKEN_AUDIENCE", "echoweave-rtc"
            ),
            session_token_max_ttl_seconds=int(
                _env("ECHOWEAVE_SESSION_TOKEN_MAX_TTL_SECONDS", "300")
            ),
            session_token_clock_skew_seconds=int(
                _env("ECHOWEAVE_SESSION_TOKEN_CLOCK_SKEW_SECONDS", "5")
            ),
            session_replay_cache_entries=int(
                _env("ECHOWEAVE_SESSION_REPLAY_CACHE_ENTRIES", "65536")
            ),
            allowed_origins=_tuple_env(
                "ECHOWEAVE_ALLOWED_ORIGINS",
                "",
            ),
            trusted_proxy_ips=_tuple_env("ECHOWEAVE_TRUSTED_PROXY_IPS", ""),
            allow_insecure_private_transport=_strict_bool_env(
                "ECHOWEAVE_ALLOW_INSECURE_PRIVATE_TRANSPORT"
            ),
            tls_certfile=_optional_path_env("ECHOWEAVE_TLS_CERTFILE"),
            tls_keyfile=_optional_path_env("ECHOWEAVE_TLS_KEYFILE"),
            allowed_personas=_tuple_env("ECHOWEAVE_ALLOWED_PERSONAS", "demo"),
            max_connections_per_ip=int(_env("ECHOWEAVE_MAX_CONNECTIONS_PER_IP", "4")),
            max_active_sessions=int(_env("ECHOWEAVE_MAX_ACTIVE_SESSIONS", "32")),
            max_ws_message_bytes=int(_env("ECHOWEAVE_MAX_WS_MESSAGE_BYTES", "262144")),
            max_text_chars=int(_env("ECHOWEAVE_MAX_TEXT_CHARS", "1000")),
            max_utterance_seconds=int(_env("ECHOWEAVE_MAX_UTTERANCE_SECONDS", "60")),
            max_session_seconds=int(_env("ECHOWEAVE_MAX_SESSION_SECONDS", "1800")),
            session_start_timeout_seconds=float(
                _env("ECHOWEAVE_SESSION_START_TIMEOUT_SECONDS", "15")
            ),
            runtime_start_timeout_seconds=float(
                _env("ECHOWEAVE_RUNTIME_START_TIMEOUT_SECONDS", "30")
            ),
            session_idle_timeout_seconds=float(
                _env("ECHOWEAVE_SESSION_IDLE_TIMEOUT_SECONDS", "300")
            ),
            websocket_send_timeout_seconds=float(
                _env("ECHOWEAVE_WEBSOCKET_SEND_TIMEOUT_SECONDS", "5")
            ),
            websocket_shutdown_timeout_seconds=float(
                _env("ECHOWEAVE_WEBSOCKET_SHUTDOWN_TIMEOUT_SECONDS", "5")
            ),
            outbound_queue_max_messages=int(
                _env("ECHOWEAVE_OUTBOUND_QUEUE_MAX_MESSAGES", "128")
            ),
            outbound_queue_max_bytes=int(
                _env("ECHOWEAVE_OUTBOUND_QUEUE_MAX_BYTES", "67108864")
            ),
            control_rate_per_second=float(
                _env("ECHOWEAVE_CONTROL_RATE_PER_SECOND", "10")
            ),
            control_rate_burst=int(_env("ECHOWEAVE_CONTROL_RATE_BURST", "20")),
            vad_backend=_env("ECHOWEAVE_VAD_BACKEND", "energy"),
            asr_backend=_env("ECHOWEAVE_ASR_BACKEND", "demo"),
            llm_backend=_env("ECHOWEAVE_LLM_BACKEND", "demo"),
            tts_backend=_env("ECHOWEAVE_TTS_BACKEND", "browser"),
            avatar_backend=_env("ECHOWEAVE_AVATAR_BACKEND", "client_lipsync"),
            qwen_model=_env("QWEN_ASR_MODEL", "Qwen/Qwen3-ASR-1.7B"),
            qwen_base_url=_env("QWEN_ASR_BASE_URL", "http://127.0.0.1:8001/v1").rstrip(
                "/"
            ),
            qwen_api_key=_env("QWEN_ASR_API_KEY", "EMPTY"),
            deepseek_api_key=_env("DEEPSEEK_API_KEY"),
            deepseek_base_url=_env(
                "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
            ).rstrip("/"),
            deepseek_model=_env("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            deepseek_thinking=_env("DEEPSEEK_THINKING", "disabled"),
            openai_compat_api_key=_env("OPENAI_COMPAT_API_KEY"),
            openai_compat_base_url=_env("OPENAI_COMPAT_BASE_URL").rstrip("/"),
            openai_compat_model=_env("OPENAI_COMPAT_MODEL"),
            openai_compat_max_tokens=int(_env("OPENAI_COMPAT_MAX_TOKENS", "800")),
            voxcpm_model=_env("VOXCPM_MODEL", "openbmb/VoxCPM2"),
            voxcpm_base_url=_env("VOXCPM_BASE_URL", "http://127.0.0.1:8002/v1").rstrip(
                "/"
            ),
            voxcpm_api_key=_env("VOXCPM_API_KEY", "EMPTY"),
            voxcpm_sample_rate=int(_env("VOXCPM_SAMPLE_RATE", "48000")),
            voxcpm_worker_token=_env("VOXCPM_WORKER_TOKEN"),
            voxcpm_worker_audience=_env(
                "VOXCPM_WORKER_AUDIENCE", "echoweave-voxcpm-worker"
            ),
            soulx_worker_token=_env("SOULX_WORKER_TOKEN"),
            soulx_worker_audience=_env(
                "SOULX_WORKER_AUDIENCE", "echoweave-soulx-worker"
            ),
            worker_assertion_ttl_seconds=int(
                _env("ECHOWEAVE_WORKER_ASSERTION_TTL_SECONDS", "120")
            ),
            model_worker_token=_env("MODEL_WORKER_TOKEN"),
            soulx_base_url=_env("SOULX_BASE_URL", "http://127.0.0.1:8003").rstrip("/"),
        )

    @property
    def normalized_trusted_proxy_ips(self) -> tuple[str, ...]:
        return _normalized_trusted_proxy_ips(self.trusted_proxy_ips)

    def validate_bind_host(
        self,
        host: str,
        *,
        tls_certfile: Path | None = None,
        tls_keyfile: Path | None = None,
    ) -> None:
        certfile = self.tls_certfile if tls_certfile is None else tls_certfile
        keyfile = self.tls_keyfile if tls_keyfile is None else tls_keyfile
        certfile, keyfile = _validated_tls_pair(certfile, keyfile)
        trusted_proxies = self.normalized_trusted_proxy_ips
        if _bind_host_is_loopback(host):
            return
        if not self.access_token and not self.session_signing_key:
            raise RuntimeError(
                "session authentication is required before binding beyond localhost"
            )
        if not (
            (certfile is not None and keyfile is not None)
            or trusted_proxies
            or self.allow_insecure_private_transport
        ):
            raise RuntimeError(
                "a non-loopback bind requires TLS, an explicit trusted proxy, or "
                "ECHOWEAVE_ALLOW_INSECURE_PRIVATE_TRANSPORT=true"
            )

    @property
    def effective_voxcpm_worker_token(self) -> str:
        return self.voxcpm_worker_token or self.model_worker_token

    @property
    def effective_soulx_worker_token(self) -> str:
        return self.soulx_worker_token or self.model_worker_token

    def validate(self) -> None:
        if type(self.allow_insecure_private_transport) is not bool:
            raise ValueError(
                "ECHOWEAVE_ALLOW_INSECURE_PRIVATE_TRANSPORT must be a boolean"
            )
        _ = self.normalized_trusted_proxy_ips
        _validated_tls_pair(self.tls_certfile, self.tls_keyfile)
        for label, value in (
            ("vad", self.vad_backend),
            ("asr", self.asr_backend),
            ("llm", self.llm_backend),
            ("tts", self.tts_backend),
            ("avatar", self.avatar_backend),
        ):
            if value not in BACKEND_CHOICES[label]:
                raise ValueError(f"unsupported {label} backend: {value}")
        if not 1 <= self.port <= 65_535:
            raise ValueError("ECHOWEAVE_PORT must be between 1 and 65535")
        if self.access_token and len(self.access_token.encode("utf-8")) < 32:
            raise ValueError("ECHOWEAVE_ACCESS_TOKEN must be at least 32 bytes")
        if (
            self.session_signing_key
            and len(self.session_signing_key.encode("utf-8")) < 32
        ):
            raise ValueError("ECHOWEAVE_SESSION_SIGNING_KEY must be at least 32 bytes")
        if not self.session_token_audience or len(self.session_token_audience) > 128:
            raise ValueError("ECHOWEAVE_SESSION_TOKEN_AUDIENCE is invalid")
        if not 1 <= self.session_token_max_ttl_seconds <= 3_600:
            raise ValueError("ECHOWEAVE_SESSION_TOKEN_MAX_TTL_SECONDS is out of range")
        if not 0 <= self.session_token_clock_skew_seconds <= 60:
            raise ValueError(
                "ECHOWEAVE_SESSION_TOKEN_CLOCK_SKEW_SECONDS is out of range"
            )
        if not 1 <= self.session_replay_cache_entries <= 1_000_000:
            raise ValueError("ECHOWEAVE_SESSION_REPLAY_CACHE_ENTRIES is out of range")
        if (
            self.model_worker_token
            and len(self.model_worker_token.encode("utf-8")) < 32
        ):
            raise ValueError("MODEL_WORKER_TOKEN must be at least 32 bytes")
        for name, token in (
            ("VOXCPM_WORKER_TOKEN", self.voxcpm_worker_token),
            ("SOULX_WORKER_TOKEN", self.soulx_worker_token),
        ):
            if token and len(token.encode("utf-8")) < 32:
                raise ValueError(f"{name} must be at least 32 bytes")
        for name, audience in (
            ("VOXCPM_WORKER_AUDIENCE", self.voxcpm_worker_audience),
            ("SOULX_WORKER_AUDIENCE", self.soulx_worker_audience),
        ):
            if not audience or len(audience) > 128:
                raise ValueError(f"{name} is invalid")
        if not 1 <= self.worker_assertion_ttl_seconds <= 300:
            raise ValueError("ECHOWEAVE_WORKER_ASSERTION_TTL_SECONDS is out of range")
        if (
            self.consent_signing_key
            and len(self.consent_signing_key.encode("utf-8")) < 32
        ):
            raise ValueError("ECHOWEAVE_CONSENT_SIGNING_KEY must be at least 32 bytes")
        if self.consent_signing_key and self.consent_state_path is None:
            raise ValueError(
                "ECHOWEAVE_CONSENT_STATE_PATH is required with a consent signing key"
            )
        if self.llm_backend == "deepseek":
            if not self.deepseek_api_key:
                raise ValueError(
                    "DEEPSEEK_API_KEY is required for the DeepSeek backend"
                )
            _validate_remote_https_base_url(
                self.deepseek_base_url,
                "DEEPSEEK_BASE_URL",
            )
        if self.llm_backend == "openai_compatible":
            if not self.openai_compat_api_key:
                raise ValueError(
                    "OPENAI_COMPAT_API_KEY is required for the OpenAI-compatible backend"
                )
            if not self.openai_compat_base_url:
                raise ValueError(
                    "OPENAI_COMPAT_BASE_URL is required for the OpenAI-compatible backend"
                )
            if not self.openai_compat_model:
                raise ValueError(
                    "OPENAI_COMPAT_MODEL is required for the OpenAI-compatible backend"
                )
            _validate_remote_https_base_url(
                self.openai_compat_base_url,
                "OPENAI_COMPAT_BASE_URL",
            )
            if not 1 <= self.openai_compat_max_tokens <= 16_384:
                raise ValueError("OPENAI_COMPAT_MAX_TOKENS is out of range")
        if self.tts_backend == "voxcpm_http" and not self.effective_voxcpm_worker_token:
            raise ValueError("VOXCPM_WORKER_TOKEN is required for the VoxCPM worker")
        if (
            self.avatar_backend == "soulx_http"
            and not self.effective_soulx_worker_token
        ):
            raise ValueError("SOULX_WORKER_TOKEN is required for the SoulX worker")
        if not 1 <= self.max_connections_per_ip <= 100:
            raise ValueError("ECHOWEAVE_MAX_CONNECTIONS_PER_IP is out of range")
        if not 1 <= self.max_active_sessions <= 10_000:
            raise ValueError("ECHOWEAVE_MAX_ACTIVE_SESSIONS is out of range")
        if not 1_024 <= self.max_ws_message_bytes <= 64 * 1024 * 1024:
            raise ValueError("ECHOWEAVE_MAX_WS_MESSAGE_BYTES is out of range")
        if not 1 <= self.max_text_chars <= 10_000:
            raise ValueError("ECHOWEAVE_MAX_TEXT_CHARS is out of range")
        if not 1 <= self.max_utterance_seconds <= 600:
            raise ValueError("ECHOWEAVE_MAX_UTTERANCE_SECONDS is out of range")
        if not 15 <= self.max_session_seconds <= 86_400:
            raise ValueError("ECHOWEAVE_MAX_SESSION_SECONDS is out of range")
        if not 0.1 <= self.session_start_timeout_seconds <= 300:
            raise ValueError("ECHOWEAVE_SESSION_START_TIMEOUT_SECONDS is out of range")
        if not 0.1 <= self.runtime_start_timeout_seconds <= 900:
            raise ValueError("ECHOWEAVE_RUNTIME_START_TIMEOUT_SECONDS is out of range")
        if not 1 <= self.session_idle_timeout_seconds <= 86_400:
            raise ValueError("ECHOWEAVE_SESSION_IDLE_TIMEOUT_SECONDS is out of range")
        if not 0.1 <= self.websocket_send_timeout_seconds <= 60:
            raise ValueError("ECHOWEAVE_WEBSOCKET_SEND_TIMEOUT_SECONDS is out of range")
        if not 0.1 <= self.websocket_shutdown_timeout_seconds <= 60:
            raise ValueError(
                "ECHOWEAVE_WEBSOCKET_SHUTDOWN_TIMEOUT_SECONDS is out of range"
            )
        if not 8 <= self.outbound_queue_max_messages <= 4_096:
            raise ValueError("ECHOWEAVE_OUTBOUND_QUEUE_MAX_MESSAGES is out of range")
        if not 1_048_576 <= self.outbound_queue_max_bytes <= 512 * 1024 * 1024:
            raise ValueError("ECHOWEAVE_OUTBOUND_QUEUE_MAX_BYTES is out of range")
        if not 0.1 <= self.control_rate_per_second <= 1_000:
            raise ValueError("ECHOWEAVE_CONTROL_RATE_PER_SECOND is out of range")
        if not 1 <= self.control_rate_burst <= 10_000:
            raise ValueError("ECHOWEAVE_CONTROL_RATE_BURST is out of range")
        for persona_id in self.allowed_personas:
            if (
                not persona_id
                or persona_id != persona_id.lower()
                or any(
                    char not in "abcdefghijklmnopqrstuvwxyz0123456789-_"
                    for char in persona_id
                )
            ):
                raise ValueError(f"invalid allowed persona ID: {persona_id}")
        if (
            any(persona_id != "demo" for persona_id in self.allowed_personas)
            and not self.session_signing_key
        ):
            raise ValueError(
                "ECHOWEAVE_SESSION_SIGNING_KEY is required for non-demo personas"
            )
        for origin in self.allowed_origins:
            if not origin.startswith(("http://", "https://")):
                raise ValueError(f"invalid allowed origin: {origin}")

    def origin_allowed(self, origin: str | None) -> bool:
        if not origin:
            return True
        local_origins = {
            f"http://127.0.0.1:{self.port}",
            f"http://localhost:{self.port}",
            f"https://127.0.0.1:{self.port}",
            f"https://localhost:{self.port}",
        }
        return origin in local_origins or origin in self.allowed_origins
