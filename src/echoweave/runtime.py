from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any

from echoweave.adapters.asr import DemoASR, Qwen3ASRHTTP, Qwen3ASRLocal
from echoweave.adapters.avatar import ClientLipSyncAvatar, SoulXHTTPAvatar
from echoweave.adapters.llm import DeepSeekV4Flash, DemoLLM, OpenAICompatibleLLM
from echoweave.adapters.tts import BrowserTTS, VoxCPM2HTTP, VoxCPM2Local
from echoweave.adapters.vad import EnergyVAD, SileroV5VAD
from echoweave.auth import issue_consent_assertion
from echoweave.config import Settings

LOGGER = logging.getLogger("echoweave.runtime")


@dataclass(slots=True)
class RuntimeAdapters:
    vad: object
    asr: object
    llm: object
    tts: object
    avatar: object


class RuntimeUnavailable(RuntimeError):
    """A safe, correlation-friendly runtime construction failure."""

    def __init__(self, reference: str) -> None:
        super().__init__(f"runtime is unavailable ({reference})")
        self.reference = reference


def _persona_reference_bytes(persona: object, kind: str) -> bytes | None:
    captured = getattr(persona, f"reference_{kind}_data", None)
    if captured is not None:
        if not isinstance(captured, bytes):
            raise TypeError(f"reference {kind} snapshot must be bytes")
        return captured
    path = getattr(persona, f"reference_{kind}", None)
    if path is None:
        return None
    return path.read_bytes()


def _consent_assertion(
    persona: object,
    *,
    signing_key: str,
    audience: str,
    scopes: set[str],
    reference_hashes: dict[str, str],
    ttl_seconds: int,
) -> str:
    persona_id = str(persona.persona_id)
    consent_id = getattr(persona, "consent_id", None)
    revision = getattr(persona, "manifest_revision", 0)
    manifest_digest = getattr(persona, "manifest_digest", "")
    if persona_id == "demo":
        consent_id = consent_id or "demo-local"
    if not isinstance(consent_id, str) or not consent_id:
        raise RuntimeError("persona consent identity is unavailable")
    if not isinstance(revision, int) or revision < 0:
        raise RuntimeError("persona consent revision is unavailable")
    if not isinstance(manifest_digest, str) or not manifest_digest:
        raise RuntimeError("persona manifest digest is unavailable")
    return issue_consent_assertion(
        signing_key,
        audience=audience,
        persona_id=persona_id,
        consent_id=consent_id,
        revision=revision,
        manifest_digest=manifest_digest,
        scopes=scopes,
        reference_hashes=reference_hashes,
        ttl_seconds=ttl_seconds,
    )


class ConsentBoundTTS:
    """Issue one short-lived, request-bound worker assertion per TTS stream."""

    browser_fallback = False

    def __init__(
        self,
        adapter: VoxCPM2HTTP,
        *,
        signing_key: str,
        audience: str,
        ttl_seconds: int,
    ) -> None:
        self.adapter = adapter
        self.signing_key = signing_key
        self.audience = audience
        self.ttl_seconds = ttl_seconds
        self._request_lock = asyncio.Lock()

    async def synthesize(self, text, persona, cancel_event):
        voice = _persona_reference_bytes(persona, "voice")
        reference_hashes = (
            {"voice": hashlib.sha256(voice).hexdigest()} if voice is not None else {}
        )
        scopes = {"voice_synthesis"}
        if voice is not None:
            scopes.add("voice_clone")
        assertion = _consent_assertion(
            persona,
            signing_key=self.signing_key,
            audience=self.audience,
            scopes=scopes,
            reference_hashes=reference_hashes,
            ttl_seconds=self.ttl_seconds,
        )
        async with self._request_lock:
            self.adapter.worker_token = assertion
            try:
                async for frame in self.adapter.synthesize(text, persona, cancel_event):
                    yield frame
            finally:
                self.adapter.worker_token = ""

    async def aclose(self) -> None:
        await self.adapter.aclose()


class ConsentBoundAvatar:
    """Issue one short-lived, reference-image-bound assertion per avatar stream."""

    synchronized_playback = True

    def __init__(
        self,
        adapter: SoulXHTTPAvatar,
        *,
        signing_key: str,
        audience: str,
        ttl_seconds: int,
    ) -> None:
        self.adapter = adapter
        self.signing_key = signing_key
        self.audience = audience
        self.ttl_seconds = ttl_seconds
        self._request_lock = asyncio.Lock()

    async def animate(
        self,
        text,
        audio_pcm16,
        sample_rate,
        persona,
        cancel_event,
    ):
        image = _persona_reference_bytes(persona, "image")
        if image is None:
            raise RuntimeError("avatar consent reference is unavailable")
        assertion = _consent_assertion(
            persona,
            signing_key=self.signing_key,
            audience=self.audience,
            scopes={"avatar_animation"},
            reference_hashes={"image": hashlib.sha256(image).hexdigest()},
            ttl_seconds=self.ttl_seconds,
        )
        async with self._request_lock:
            self.adapter.worker_token = assertion
            try:
                async for segment in self.adapter.animate(
                    text,
                    audio_pcm16,
                    sample_rate,
                    persona,
                    cancel_event,
                ):
                    yield segment
            finally:
                self.adapter.worker_token = ""

    async def aclose(self) -> None:
        await self.adapter.aclose()


def runtime_capabilities(settings: Settings) -> tuple[str, ...]:
    """Return stable wire capabilities without exposing credentials or endpoints."""

    capabilities = [
        "input.audio_pcm16",
        "input.text",
        "output.text_stream",
        "control.barge_in",
        "control.cancel",
        "control.ping",
        "identity.ai_disclosure",
    ]
    if settings.tts_backend == "browser":
        capabilities.append("output.browser_tts")
    else:
        capabilities.append("output.audio_pcm16")
    if settings.avatar_backend == "client_lipsync":
        capabilities.append("output.avatar_events")
    else:
        capabilities.extend(("output.audio_pcm16", "output.video_fragments"))
    return tuple(dict.fromkeys(capabilities))


def runtime_backends(settings: Settings) -> dict[str, str]:
    """Return public backend identifiers without endpoints, models, or secrets."""

    return {
        "vad": settings.vad_backend,
        "asr": settings.asr_backend,
        "llm": settings.llm_backend,
        "tts": settings.tts_backend,
        "avatar": settings.avatar_backend,
    }


class RuntimeFactory:
    """Single-flight runtime construction with deterministic result ownership.

    Adapter construction can involve disk I/O, model verification, or downloads,
    so it runs outside the event loop and cannot be force-cancelled safely.  One
    factory therefore owns at most one construction task.  Concurrent callers
    share that task, but only one caller may claim the resulting adapters; any
    remaining live callers start the next isolated runtime only after the first
    result has been claimed.  Cancelled and timed-out callers never enqueue more
    background builds.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._state_lock = asyncio.Lock()
        self._prepared: RuntimeAdapters | None = None
        self._build_task: asyncio.Task[RuntimeAdapters] | None = None
        self._build_reference: str | None = None
        self._adapter_cleanup_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closing = False
        self._state = "starting"
        self._last_success_at: float | None = None
        self._last_failure_at: float | None = None
        self._last_error_reference: str | None = None

    @staticmethod
    def _consume_build_result(task: asyncio.Task[RuntimeAdapters]) -> None:
        """Retrieve background failures when every request stopped waiting."""

        if task.cancelled():
            return
        try:
            task.exception()
        except (asyncio.CancelledError, asyncio.InvalidStateError):
            pass

    def _cleanup_completed(self, task: asyncio.Task[None]) -> None:
        if self._adapter_cleanup_task is task:
            self._adapter_cleanup_task = None
        if task.cancelled():
            return
        try:
            task.exception()
        except (asyncio.CancelledError, asyncio.InvalidStateError):
            pass

    def _has_owned_resources(self) -> bool:
        return bool(
            self._prepared is not None
            or (self._build_task is not None and not self._build_task.done())
            or (
                self._adapter_cleanup_task is not None
                and not self._adapter_cleanup_task.done()
            )
        )

    async def _close_adapters(self, adapters: RuntimeAdapters) -> None:
        seen: set[int] = set()
        for adapter in (
            adapters.vad,
            adapters.asr,
            adapters.llm,
            adapters.tts,
            adapters.avatar,
        ):
            if id(adapter) in seen:
                continue
            seen.add(id(adapter))
            async_close = getattr(adapter, "aclose", None)
            sync_close = getattr(adapter, "close", None)
            close = async_close if callable(async_close) else sync_close
            if not callable(close):
                continue
            try:
                if callable(async_close):
                    result = async_close()
                    if not inspect.isawaitable(result):
                        raise TypeError("adapter aclose() must return an awaitable")
                else:
                    result = await asyncio.to_thread(close)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                LOGGER.exception("runtime adapter cleanup failed")

    async def _run_build(self, reference: str) -> RuntimeAdapters:
        """Build once, then atomically publish or close the owned result."""

        current = asyncio.current_task()
        try:
            adapters = await asyncio.to_thread(build_runtime, self.settings)
            if not isinstance(adapters, RuntimeAdapters):
                raise TypeError("runtime builder must return RuntimeAdapters")
        except asyncio.CancelledError:
            async with self._state_lock:
                if self._build_task is current:
                    self._build_task = None
                    self._build_reference = None
                if self._closing:
                    self._state = "closed"
                else:
                    self._state = "unavailable"
                    self._last_failure_at = time.time()
                    self._last_error_reference = reference
            raise
        except Exception:
            async with self._state_lock:
                if self._build_task is current:
                    self._build_task = None
                    self._build_reference = None
                if self._closing:
                    self._state = "closed"
                else:
                    self._state = "unavailable"
                    self._last_failure_at = time.time()
                    self._last_error_reference = reference
            LOGGER.exception("runtime construction failed reference=%s", reference)
            raise

        close_result = False
        async with self._state_lock:
            if self._closing:
                self._state = "closed"
                close_result = True
            elif self._prepared is None:
                self._prepared = adapters
                self._state = "ready"
                self._last_success_at = time.time()
                self._last_error_reference = None
                if self._build_task is current:
                    self._build_task = None
                    self._build_reference = None
            else:
                # The state machine permits one published result and one in-flight
                # task at most.  Keep this defensive branch leak-free if a future
                # change violates that invariant.
                close_result = True

        if close_result:
            try:
                await self._close_adapters(adapters)
            finally:
                async with self._state_lock:
                    if self._build_task is current:
                        self._build_task = None
                        self._build_reference = None
        return adapters

    async def _claim_or_start_build(
        self,
    ) -> tuple[RuntimeAdapters | None, asyncio.Task[RuntimeAdapters] | None, str]:
        """Return a prepared runtime or the factory's one shared build task."""

        async with self._state_lock:
            if self._closing:
                raise RuntimeUnavailable("rt_factory_closing")
            if self._prepared is not None:
                prepared = self._prepared
                self._prepared = None
                return prepared, None, ""
            task = self._build_task
            reference = self._build_reference
            if task is None:
                reference = f"rt_{uuid.uuid4().hex[:12]}"
                task = asyncio.create_task(
                    self._run_build(reference),
                    name=f"echoweave-runtime-{reference}",
                )
                self._build_task = task
                self._build_reference = reference
                task.add_done_callback(self._consume_build_result)
            if reference is None:  # Defensive invariant for future refactors.
                raise RuntimeError("runtime build reference is unavailable")
            return None, task, reference

    async def create(self, timeout_seconds: float) -> RuntimeAdapters:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        deadline = time.monotonic() + timeout_seconds
        while True:
            prepared, task, reference = await self._claim_or_start_build()
            if prepared is not None:
                return prepared
            if task is None:  # Defensive invariant for static type narrowing.
                raise RuntimeError("runtime build task is unavailable")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                async with self._state_lock:
                    if not self._closing:
                        self._state = "unavailable"
                        self._last_failure_at = time.time()
                        self._last_error_reference = reference
                LOGGER.error("runtime construction timed out reference=%s", reference)
                raise RuntimeUnavailable(reference)
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except asyncio.CancelledError:
                # The factory task owns the uninterruptible thread work.  Caller
                # cancellation drops only this waiter and cannot spawn retention
                # tasks or release another construction behind it.
                raise
            except TimeoutError as exc:
                async with self._state_lock:
                    if not self._closing:
                        self._state = "unavailable"
                        self._last_failure_at = time.time()
                        self._last_error_reference = reference
                LOGGER.error("runtime construction timed out reference=%s", reference)
                raise RuntimeUnavailable(reference) from exc
            except Exception as exc:
                raise RuntimeUnavailable(reference) from exc
            # Every waiter observes the same task result.  Exactly one claims the
            # published adapters on the next loop; other live waiters then share
            # the next single-flight build rather than the same stateful runtime.

    async def prepare(self, timeout_seconds: float) -> None:
        """Warm adapter construction while keeping liveness independent."""

        try:
            adapters = await self.create(timeout_seconds)
            close_result = False
            async with self._state_lock:
                if self._closing:
                    close_result = True
                elif self._prepared is None:
                    self._prepared = adapters
                else:
                    close_result = True
            if close_result:
                await self._close_adapters(adapters)
        except RuntimeUnavailable:
            # The process remains live so operators can inspect readiness and logs.
            return

    async def acquire(self, timeout_seconds: float) -> RuntimeAdapters:
        """Return the warmed runtime once, then build isolated session adapters."""

        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        return await self.create(timeout_seconds)

    async def release(
        self,
        adapters: RuntimeAdapters,
        timeout_seconds: float = 30.0,
    ) -> bool:
        """Close adapters that were acquired but never attached to a session."""

        if not isinstance(adapters, RuntimeAdapters):
            raise TypeError("adapters must be RuntimeAdapters")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        try:
            await asyncio.wait_for(
                self._close_adapters(adapters),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            LOGGER.error("runtime release timed out")
            return False
        return True

    async def aclose(self, timeout_seconds: float = 30.0) -> None:
        """Close warmed adapters and reclaim timed-out background builds."""

        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        # Set the terminal state synchronously so no new create/acquire call can
        # slip in before the close task obtains the state lock.
        self._closing = True
        self._state = "closed"
        deadline = time.monotonic() + timeout_seconds
        while self._has_owned_resources():
            task = self._close_task
            if task is None or task.done():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                task = asyncio.create_task(
                    self._aclose_impl(remaining),
                    name="echoweave-runtime-factory-shutdown",
                )
                self._close_task = task
            # Shielding keeps shutdown-owned adapter cleanup alive if its
            # initiating ASGI task is cancelled.  A later call awaits the same
            # task and, if its budget expired, continues with another bounded wait.
            await asyncio.shield(task)
            if time.monotonic() >= deadline:
                return

    async def _aclose_impl(self, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        async with self._state_lock:
            build_task = self._build_task
            cleanup_task = self._adapter_cleanup_task
            if cleanup_task is None and self._prepared is not None:
                prepared = self._prepared
                self._prepared = None
                cleanup_task = asyncio.create_task(
                    self._close_adapters(prepared),
                    name="echoweave-runtime-adapter-cleanup",
                )
                self._adapter_cleanup_task = cleanup_task
                cleanup_task.add_done_callback(self._cleanup_completed)
        owned_tasks = {
            task
            for task in (build_task, cleanup_task)
            if task is not None and not task.done()
        }
        if not owned_tasks:
            return
        _, pending = await asyncio.wait(
            owned_tasks,
            timeout=max(0.0, deadline - time.monotonic()),
        )
        if pending:
            LOGGER.error(
                "runtime shutdown budget expired with %d owned task(s)",
                len(pending),
            )

    def readiness(self) -> dict[str, Any]:
        return {
            "ready": self._state == "ready",
            "state": self._state,
            "readiness_scope": "adapter_construction",
            "dependency_reachability": "not_probed",
            "last_success_at_unix": self._last_success_at,
            "last_failure_at_unix": self._last_failure_at,
            "error_reference": self._last_error_reference,
        }


def build_runtime(settings: Settings) -> RuntimeAdapters:
    vad = SileroV5VAD() if settings.vad_backend == "silero_v5" else EnergyVAD()
    if settings.asr_backend == "qwen_local":
        asr = Qwen3ASRLocal(settings.qwen_model)
    elif settings.asr_backend == "qwen_http":
        asr = Qwen3ASRHTTP(
            settings.qwen_base_url,
            settings.qwen_model,
            settings.qwen_api_key,
        )
    else:
        asr = DemoASR()

    if settings.llm_backend == "deepseek":
        llm = DeepSeekV4Flash(
            settings.deepseek_api_key,
            settings.deepseek_base_url,
            settings.deepseek_model,
            settings.deepseek_thinking,
        )
    elif settings.llm_backend == "openai_compatible":
        llm = OpenAICompatibleLLM(
            settings.openai_compat_api_key,
            settings.openai_compat_base_url,
            settings.openai_compat_model,
            max_tokens=settings.openai_compat_max_tokens,
        )
    else:
        llm = DemoLLM()

    if settings.tts_backend == "voxcpm_local":
        tts = VoxCPM2Local(settings.voxcpm_model)
    elif settings.tts_backend == "voxcpm_http":
        tts = ConsentBoundTTS(
            VoxCPM2HTTP(
                settings.voxcpm_base_url,
                settings.voxcpm_model,
                settings.voxcpm_api_key,
                settings.voxcpm_sample_rate,
            ),
            signing_key=settings.effective_voxcpm_worker_token,
            audience=settings.voxcpm_worker_audience,
            ttl_seconds=settings.worker_assertion_ttl_seconds,
        )
    else:
        tts = BrowserTTS()

    if settings.avatar_backend == "soulx_http":
        avatar = ConsentBoundAvatar(
            SoulXHTTPAvatar(settings.soulx_base_url),
            signing_key=settings.effective_soulx_worker_token,
            audience=settings.soulx_worker_audience,
            ttl_seconds=settings.worker_assertion_ttl_seconds,
        )
    else:
        avatar = ClientLipSyncAvatar()
    return RuntimeAdapters(vad, asr, llm, tts, avatar)
