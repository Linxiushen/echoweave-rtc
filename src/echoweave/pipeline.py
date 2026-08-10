from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import math
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from echoweave.chunking import SemanticChunker
from echoweave.contracts import AudioFrame, PersonaProfile, SessionState
from echoweave.observability import MetricRegistry, Observability
from echoweave.protocol import PacketKind, pack_packet
from echoweave.runtime import RuntimeAdapters

EmitJSON = Callable[[dict[str, Any]], Awaitable[None]]
EmitBinary = Callable[[bytes], Awaitable[None]]
AuthorizationCheck = Callable[[], None]
LOGGER = logging.getLogger("echoweave.pipeline")
MAX_PHRASE_AUDIO_BYTES = 16 * 1024 * 1024
_QUEUE_END = object()
_ALLOWED_STATE_TRANSITIONS = {
    SessionState.READY: {SessionState.LISTENING, SessionState.CLOSED},
    SessionState.LISTENING: {
        SessionState.LISTENING,
        SessionState.USER_SPEAKING,
        SessionState.TRANSCRIBING,
        SessionState.THINKING,
        SessionState.CLOSED,
    },
    SessionState.USER_SPEAKING: {
        SessionState.USER_SPEAKING,
        SessionState.LISTENING,
        SessionState.TRANSCRIBING,
        SessionState.THINKING,
        SessionState.CLOSED,
    },
    SessionState.TRANSCRIBING: {
        SessionState.TRANSCRIBING,
        SessionState.LISTENING,
        SessionState.USER_SPEAKING,
        SessionState.THINKING,
        SessionState.CLOSED,
    },
    SessionState.THINKING: {
        SessionState.THINKING,
        SessionState.LISTENING,
        SessionState.USER_SPEAKING,
        SessionState.SPEAKING,
        SessionState.CLOSED,
    },
    SessionState.SPEAKING: {
        SessionState.SPEAKING,
        SessionState.LISTENING,
        SessionState.USER_SPEAKING,
        SessionState.TRANSCRIBING,
        SessionState.THINKING,
        SessionState.CLOSED,
    },
    SessionState.CLOSED: {SessionState.CLOSED},
}


class StageTimeout(TimeoutError):
    """Raised when a realtime stage exceeds its latency budget."""

    def __init__(self, stage: str) -> None:
        super().__init__(f"realtime stage timed out: {stage}")
        self.stage = stage


class SlowConsumerError(RuntimeError):
    """Raised when outbound delivery cannot keep up with the realtime session."""


class ResponseLimitError(RuntimeError):
    """Raised when an upstream model exceeds the bounded response size."""


class AuthorizationFailure(RuntimeError):
    """Raised when a session's server-side persona authorization is no longer valid."""


class RealtimeSession:
    """EchoWeave's turn engine with endpointing, streaming and barge-in."""

    def __init__(
        self,
        session_id: str,
        persona: PersonaProfile,
        adapters: RuntimeAdapters,
        emit_json: EmitJSON,
        emit_binary: EmitBinary,
        sample_rate: int = 16_000,
        authorization_check: AuthorizationCheck | None = None,
        max_utterance_seconds: int = 60,
        max_text_chars: int = 1_000,
        max_turns: int = 100,
        authorization_check_interval: float = 0.25,
        speech_queue_size: int = 4,
        max_response_chars: int = 16_000,
        end_to_end_timeout: float = 120.0,
        asr_timeout: float = 30.0,
        llm_first_token_timeout: float = 15.0,
        llm_idle_timeout: float = 20.0,
        speech_backpressure_timeout: float = 8.0,
        tts_phrase_timeout: float = 45.0,
        avatar_phrase_timeout: float = 45.0,
        emit_timeout: float = 5.0,
        cancellation_timeout: float = 2.0,
        adapter_close_timeout: float = 5.0,
        observability: Observability | MetricRegistry | None = None,
    ) -> None:
        numeric_limits = {
            "sample_rate": sample_rate,
            "max_utterance_seconds": max_utterance_seconds,
            "max_text_chars": max_text_chars,
            "max_turns": max_turns,
            "speech_queue_size": speech_queue_size,
            "max_response_chars": max_response_chars,
        }
        for name, value in numeric_limits.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        timeout_limits = {
            "end_to_end_timeout": end_to_end_timeout,
            "asr_timeout": asr_timeout,
            "llm_first_token_timeout": llm_first_token_timeout,
            "llm_idle_timeout": llm_idle_timeout,
            "speech_backpressure_timeout": speech_backpressure_timeout,
            "tts_phrase_timeout": tts_phrase_timeout,
            "avatar_phrase_timeout": avatar_phrase_timeout,
            "emit_timeout": emit_timeout,
            "cancellation_timeout": cancellation_timeout,
            "adapter_close_timeout": adapter_close_timeout,
        }
        for name, value in timeout_limits.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be positive")
        if (
            isinstance(authorization_check_interval, bool)
            or not isinstance(authorization_check_interval, (int, float))
            or not math.isfinite(authorization_check_interval)
            or authorization_check_interval < 0
        ):
            raise ValueError("authorization_check_interval must be non-negative")
        self.session_id = session_id
        self.persona = persona
        self.adapters = adapters
        self.emit_json = emit_json
        self.emit_binary = emit_binary
        self.sample_rate = sample_rate
        self.authorization_check = authorization_check
        self.max_utterance_bytes = sample_rate * 2 * max_utterance_seconds
        self.max_text_chars = max_text_chars
        self.max_turns = max_turns
        self.authorization_check_interval = authorization_check_interval
        self.speech_queue_size = speech_queue_size
        self.max_response_chars = max_response_chars
        self.end_to_end_timeout = float(end_to_end_timeout)
        self.asr_timeout = float(asr_timeout)
        self.llm_first_token_timeout = float(llm_first_token_timeout)
        self.llm_idle_timeout = float(llm_idle_timeout)
        self.speech_backpressure_timeout = float(speech_backpressure_timeout)
        self.tts_phrase_timeout = float(tts_phrase_timeout)
        self.avatar_phrase_timeout = float(avatar_phrase_timeout)
        self.emit_timeout = float(emit_timeout)
        self.cancellation_timeout = float(cancellation_timeout)
        self.adapter_close_timeout = float(adapter_close_timeout)
        if isinstance(observability, Observability):
            self.metrics: MetricRegistry | None = observability.metrics
        elif isinstance(observability, MetricRegistry) or observability is None:
            self.metrics = observability
        else:
            raise TypeError(
                "observability must be Observability, MetricRegistry, or None"
            )
        self._last_authorization_check = 0.0
        self.state = SessionState.READY
        self.turn_id = 0
        self.generation_id = 0
        self._speech_active = False
        self._utterance = bytearray()
        self._pre_roll: deque[bytes] = deque(maxlen=8)
        self._response_task: asyncio.Task | None = None
        self._cancel_event = asyncio.Event()
        self._turn_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._adapter_close_lock = asyncio.Lock()
        self._adapters_closed = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._retired_tasks: set[asyncio.Future[Any]] = set()
        self._history: list[dict[str, str]] = [
            {"role": "system", "content": persona.system_prompt}
        ]

    async def start(self) -> None:
        async with self._turn_lock:
            if self.state is SessionState.CLOSED:
                return
            if self.state is not SessionState.READY:
                raise RuntimeError("session has already been started")
            await self._set_state(SessionState.LISTENING)
            await self._emit_json(
                {
                    "type": "session.ready",
                    "session_id": self.session_id,
                    "persona_id": self.persona.persona_id,
                    "persona": self.persona.display_name,
                    "disclosure": self.persona.disclosure_text,
                    "synthetic": True,
                    "fictional": self.persona.is_fictional,
                }
            )

    async def ingest_pcm(self, pcm16: bytes, sample_rate: int) -> None:
        if self.state is SessionState.CLOSED:
            return
        if self.state is SessionState.READY:
            await self._error("session_not_started", "start the session before media")
            return
        if sample_rate != self.sample_rate:
            await self._error(
                "invalid_audio_rate",
                f"expected {self.sample_rate} Hz PCM, got {sample_rate} Hz",
            )
            return
        if not pcm16 or len(pcm16) % 2:
            await self._error(
                "invalid_audio_frame",
                "audio frames must contain an even number of PCM16 bytes",
            )
            return
        self._pre_roll.append(pcm16)
        vad_started = time.perf_counter()
        try:
            decision = await self.adapters.vad.process(pcm16, sample_rate)
        except Exception as exc:  # noqa: BLE001 - VAD is an adapter boundary
            self._observe_elapsed("vad", vad_started, "error")
            self.adapters.vad.reset()
            await self._internal_error("vad_failed", exc)
            await self._set_state(SessionState.LISTENING)
            return
        self._observe_elapsed("vad", vad_started, "success")
        await self._emit_json(
            {
                "type": "vad.level",
                "probability": round(decision.probability, 4),
            }
        )
        if decision.speech_started and not self._speech_active:
            self._speech_active = True
            self._utterance = bytearray(b"".join(self._pre_roll))
            await self.cancel_response("barge_in")
            await self._set_state(SessionState.USER_SPEAKING)
            await self._emit_json({"type": "vad.speech_started"})
        elif self._speech_active:
            self._utterance.extend(pcm16)
            if len(self._utterance) > self.max_utterance_bytes:
                self._speech_active = False
                self._utterance.clear()
                self.adapters.vad.reset()
                await self._error(
                    "utterance_too_long",
                    "speech exceeded the configured duration limit",
                )
                await self._set_state(SessionState.LISTENING)
                return

        if decision.speech_ended and self._speech_active:
            self._speech_active = False
            audio = bytes(self._utterance)
            self._utterance.clear()
            self.adapters.vad.reset()
            await self._emit_json({"type": "vad.speech_ended"})
            await self._start_audio_turn(audio)

    async def submit_text(self, text: str) -> None:
        if self.state is SessionState.CLOSED:
            return
        if self.state is SessionState.READY:
            await self._error("session_not_started", "start the session before a turn")
            return
        text = text.strip()
        if not text:
            return
        if len(text) > self.max_text_chars:
            await self._error("text_too_long", "text turn exceeds the configured limit")
            return
        await self._begin_turn(text=text, pcm16=None)

    async def _start_audio_turn(self, pcm16: bytes) -> None:
        await self._begin_turn(text=None, pcm16=pcm16)

    async def _begin_turn(self, *, text: str | None, pcm16: bytes | None) -> None:
        reason = "new_text_turn" if text is not None else "new_audio_turn"
        async with self._turn_lock:
            if self.state is SessionState.CLOSED:
                return
            if self.turn_id >= self.max_turns:
                await self._error("turn_limit", "session turn limit reached")
                return
            try:
                self._authorize(force=True)
            except AuthorizationFailure:
                await self._cancel_active_locked("authorization_revoked")
                await self._error(
                    "authorization_revoked",
                    "persona authorization is no longer valid",
                )
                await self._set_state(SessionState.LISTENING)
                return

            await self._cancel_active_locked(reason)
            if text is not None:
                self._speech_active = False
                self._utterance.clear()
                self._pre_roll.clear()
                self.adapters.vad.reset()

            self.turn_id += 1
            turn_id = self.turn_id
            self.generation_id += 1
            generation_id = self.generation_id
            cancel_event = asyncio.Event()
            self._cancel_event = cancel_event
            deadline = time.monotonic() + self.end_to_end_timeout
            if text is not None:
                coroutine = self._respond(
                    turn_id,
                    generation_id,
                    text,
                    cancel_event,
                    deadline,
                )
                name = f"echoweave-text-{self.session_id}-{turn_id}"
            else:
                if pcm16 is None:  # Defensive invariant for future callers.
                    raise RuntimeError("audio turn requires PCM input")
                coroutine = self._transcribe_and_respond(
                    turn_id,
                    generation_id,
                    pcm16,
                    cancel_event,
                    deadline,
                )
                name = f"echoweave-audio-{self.session_id}-{turn_id}"
            task = asyncio.create_task(coroutine, name=name)
            self._response_task = task
            task.add_done_callback(self._response_done)

    async def _transcribe_and_respond(
        self,
        turn_id: int,
        generation_id: int,
        pcm16: bytes,
        cancel_event: asyncio.Event,
        deadline: float,
    ) -> None:
        started = time.perf_counter()
        try:
            if not await self._set_state(
                SessionState.TRANSCRIBING,
                turn_id,
                generation_id=generation_id,
                cancel_event=cancel_event,
            ):
                return
            transcript = await self._await_bounded(
                self.adapters.asr.transcribe(pcm16, self.sample_rate),
                self._stage_timeout(deadline, self.asr_timeout, "asr"),
                "asr",
            )
            if not self._is_current(generation_id, cancel_event):
                return
            self._observe_elapsed("asr", started, "success")
            text = transcript.text.strip()
            await self._emit_json(
                {
                    "type": "asr.final",
                    "turn_id": turn_id,
                    "text": text,
                    "language": transcript.language,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                },
                deadline=deadline,
                generation_id=generation_id,
                cancel_event=cancel_event,
            )
            if not text:
                await self._error(
                    "empty_transcript",
                    "ASR returned no text",
                    turn_id,
                    deadline=deadline,
                )
                await self._set_state(
                    SessionState.LISTENING,
                    turn_id,
                    generation_id=generation_id,
                    cancel_event=cancel_event,
                )
                return
            await self._respond(
                turn_id,
                generation_id,
                text,
                cancel_event,
                deadline,
            )
        except asyncio.CancelledError:
            raise
        except StageTimeout as exc:
            if exc.stage == "asr":
                self._observe_elapsed("asr", started, "timeout")
            await self._handle_stage_timeout(
                exc.stage, turn_id, generation_id, cancel_event
            )
        except SlowConsumerError:
            await self._quarantine_slow_consumer(generation_id, cancel_event)
        except Exception as exc:  # noqa: BLE001 - adapter boundary must degrade safely
            if self._is_current(generation_id, cancel_event):
                self._observe_elapsed("asr", started, "error")
                try:
                    await self._internal_error("asr_failed", exc, turn_id)
                    await self._set_state(
                        SessionState.LISTENING,
                        turn_id,
                        generation_id=generation_id,
                        cancel_event=cancel_event,
                    )
                except SlowConsumerError:
                    await self._quarantine_slow_consumer(generation_id, cancel_event)

    async def _respond(
        self,
        turn_id: int,
        generation_id: int,
        user_text: str,
        cancel_event: asyncio.Event,
        deadline: float,
    ) -> None:
        assistant_text = ""
        started_at = deadline - self.end_to_end_timeout
        first_token_at: float | None = None
        text_complete_at: float | None = None
        speech_queue: asyncio.Queue[str | object] = asyncio.Queue(
            maxsize=self.speech_queue_size
        )
        speech_task = asyncio.create_task(
            self._speech_worker(
                speech_queue,
                turn_id,
                generation_id,
                cancel_event,
                deadline,
            ),
            name=f"echoweave-speech-{self.session_id}-{turn_id}",
        )
        stream_iterator: AsyncIterator[str] | None = None
        llm_started_at: float | None = None
        try:
            self._authorize(force=True)
            self._history.append({"role": "user", "content": user_text})
            if not await self._set_state(
                SessionState.THINKING,
                turn_id,
                generation_id=generation_id,
                cancel_event=cancel_event,
            ):
                return
            chunker = SemanticChunker()
            llm_started_at = time.perf_counter()
            stream_iterator = aiter(
                self.adapters.llm.stream(list(self._history), cancel_event)
            )
            while True:
                stage = "llm_first_token" if first_token_at is None else "llm_stream"
                stage_cap = (
                    self.llm_first_token_timeout
                    if first_token_at is None
                    else self.llm_idle_timeout
                )
                try:
                    delta = await self._await_bounded(
                        anext(stream_iterator),
                        self._stage_timeout(deadline, stage_cap, stage),
                        stage,
                    )
                except StopAsyncIteration:
                    break
                if not self._is_current(generation_id, cancel_event):
                    return
                self._authorize()
                if not isinstance(delta, str):
                    raise TypeError("LLM stream yielded a non-string delta")
                if not delta:
                    continue
                if first_token_at is None:
                    first_token_at = time.monotonic()
                    self._observe_elapsed("llm_first_token", llm_started_at, "success")
                if len(assistant_text) + len(delta) > self.max_response_chars:
                    raise ResponseLimitError(
                        "LLM response exceeded the character limit"
                    )
                assistant_text += delta
                await self._emit_json(
                    {
                        "type": "assistant.delta",
                        "turn_id": turn_id,
                        "generation_id": generation_id,
                        "text": delta,
                    },
                    deadline=deadline,
                    generation_id=generation_id,
                    cancel_event=cancel_event,
                )
                for phrase in chunker.push(delta):
                    await self._queue_speech(
                        speech_queue,
                        phrase,
                        speech_task,
                        deadline,
                        generation_id,
                        cancel_event,
                    )
            tail = chunker.flush()
            if tail and self._is_current(generation_id, cancel_event):
                await self._queue_speech(
                    speech_queue,
                    tail,
                    speech_task,
                    deadline,
                    generation_id,
                    cancel_event,
                )
            await self._queue_speech(
                speech_queue,
                _QUEUE_END,
                speech_task,
                deadline,
                generation_id,
                cancel_event,
            )
            if not self._is_current(generation_id, cancel_event):
                return
            text_complete_at = time.monotonic()
            assistant_text = assistant_text.strip()
            self._history.append({"role": "assistant", "content": assistant_text})
            self._history = [self._history[0], *self._history[1:][-12:]]
            await self._emit_json(
                {
                    "type": "assistant.final",
                    "turn_id": turn_id,
                    "generation_id": generation_id,
                    "text": assistant_text,
                },
                deadline=deadline,
                generation_id=generation_id,
                cancel_event=cancel_event,
            )
            await self._await_existing_task(
                speech_task,
                self._stage_timeout(deadline, None, "end_to_end"),
                "end_to_end",
            )
            if not self._is_current(generation_id, cancel_event):
                return
            completed_at = time.monotonic()
            self._observe_elapsed("turn", started_at, "success", monotonic=True)
            await self._emit_json(
                {
                    "type": "turn.metrics",
                    "turn_id": turn_id,
                    "generation_id": generation_id,
                    "first_token_ms": (
                        round((first_token_at - started_at) * 1_000)
                        if first_token_at is not None
                        else None
                    ),
                    "text_complete_ms": (
                        round((text_complete_at - started_at) * 1_000)
                        if text_complete_at is not None
                        else None
                    ),
                    "end_to_end_ms": round((completed_at - started_at) * 1_000),
                    "speech_queue_capacity": self.speech_queue_size,
                },
                deadline=deadline,
                generation_id=generation_id,
                cancel_event=cancel_event,
            )
            await self._set_state(
                SessionState.LISTENING,
                turn_id,
                generation_id=generation_id,
                cancel_event=cancel_event,
            )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._emit_json(
                    {
                        "type": "turn.cancelled",
                        "turn_id": turn_id,
                        "generation_id": generation_id,
                    }
                )
            raise
        except AuthorizationFailure:
            await self._handle_authorization_failure(
                turn_id, generation_id, cancel_event
            )
        except StageTimeout as exc:
            if exc.stage == "llm_first_token" and llm_started_at is not None:
                self._observe_elapsed("llm_first_token", llm_started_at, "timeout")
            self._observe_elapsed("turn", started_at, "timeout", monotonic=True)
            await self._handle_stage_timeout(
                exc.stage, turn_id, generation_id, cancel_event
            )
        except ResponseLimitError:
            await self._abort_generation(
                generation_id,
                cancel_event,
                "response_limit",
                error_code="response_too_long",
                error_message="assistant response exceeded the configured limit",
                turn_id=turn_id,
            )
        except SlowConsumerError:
            await self._quarantine_slow_consumer(generation_id, cancel_event)
        except Exception as exc:  # noqa: BLE001 - adapter boundary must degrade safely
            if self._is_current(generation_id, cancel_event):
                try:
                    reference = self._log_exception("generation_failed", exc)
                    await self._abort_generation(
                        generation_id,
                        cancel_event,
                        "generation_failed",
                        error_code="generation_failed",
                        error_message=f"component failed; reference {reference}",
                        turn_id=turn_id,
                    )
                except SlowConsumerError:
                    await self._quarantine_slow_consumer(generation_id, cancel_event)
        finally:
            if stream_iterator is not None:
                await self._close_async_iterator(stream_iterator, "llm_stream")
            await self._stop_task(speech_task, "speech_worker")

    async def _speech_worker(
        self,
        queue: asyncio.Queue[str | object],
        turn_id: int,
        generation_id: int,
        cancel_event: asyncio.Event,
        deadline: float,
    ) -> None:
        while self._is_current(generation_id, cancel_event):
            phrase = await queue.get()
            try:
                if phrase is _QUEUE_END:
                    return
                if not isinstance(phrase, str):
                    raise TypeError("speech queue contained an invalid item")
                await self._speak_phrase(
                    phrase,
                    turn_id,
                    generation_id,
                    cancel_event,
                    deadline,
                )
            finally:
                queue.task_done()

    async def _speak_phrase(
        self,
        phrase: str,
        turn_id: int,
        generation_id: int,
        cancel_event: asyncio.Event,
        deadline: float,
    ) -> None:
        if not self._is_current(generation_id, cancel_event):
            return
        self._authorize()
        if not await self._set_state(
            SessionState.SPEAKING,
            turn_id,
            generation_id=generation_id,
            cancel_event=cancel_event,
        ):
            return
        audio = bytearray()
        audio_frames: list[AudioFrame] = []
        sample_rate = 0
        sent_format = False
        tts_failed = False
        synchronized_avatar = bool(
            getattr(self.adapters.avatar, "synchronized_playback", False)
        )
        tts_started_at = time.perf_counter()
        tts_iterator: AsyncIterator[AudioFrame] | None = None
        try:
            tts_iterator = aiter(
                self.adapters.tts.synthesize(phrase, self.persona, cancel_event)
            )
            phrase_deadline = min(deadline, time.monotonic() + self.tts_phrase_timeout)
            while True:
                try:
                    frame = await self._await_bounded(
                        anext(tts_iterator),
                        self._stage_timeout(phrase_deadline, None, "tts"),
                        "tts",
                    )
                except StopAsyncIteration:
                    break
                if not self._is_current(generation_id, cancel_event):
                    return
                self._authorize()
                if (
                    not isinstance(frame, AudioFrame)
                    or not frame.pcm
                    or frame.sample_rate <= 0
                    or frame.channels <= 0
                    or frame.pts_ms < 0
                ):
                    raise ValueError("TTS returned an invalid audio frame")
                sample_rate = frame.sample_rate
                if not sent_format:
                    await self._emit_json(
                        {
                            "type": "tts.format",
                            "turn_id": turn_id,
                            "generation_id": generation_id,
                            "sample_rate": frame.sample_rate,
                            "channels": frame.channels,
                            "codec": frame.codec,
                        },
                        deadline=deadline,
                        generation_id=generation_id,
                        cancel_event=cancel_event,
                    )
                    sent_format = True
                audio.extend(frame.pcm)
                if len(audio) > MAX_PHRASE_AUDIO_BYTES:
                    raise RuntimeError("TTS phrase exceeded the audio buffer limit")
                audio_frames.append(frame)
                if not synchronized_avatar:
                    packet = pack_packet(
                        PacketKind.TTS_PCM16, turn_id, frame.pts_ms, frame.pcm
                    )
                    await self._emit_binary(
                        packet,
                        deadline=deadline,
                        generation_id=generation_id,
                        cancel_event=cancel_event,
                    )
        except AuthorizationFailure:
            raise
        except SlowConsumerError:
            raise
        except StageTimeout:
            self._observe_elapsed("tts", tts_started_at, "timeout")
            raise
        except Exception as exc:  # noqa: BLE001 - TTS failure triggers safe fallback
            self._observe_elapsed("tts", tts_started_at, "error")
            tts_failed = True
            audio.clear()
            sample_rate = 0
            reference = self._log_exception("tts", exc)
            await self._emit_json(
                {
                    "type": "playout.clear",
                    "reason": "tts_fallback",
                    "preserve_transcript": True,
                },
                deadline=deadline,
                generation_id=generation_id,
                cancel_event=cancel_event,
            )
            await self._emit_json(
                {
                    "type": "degraded",
                    "component": "tts",
                    "reason": f"internal error ({reference})",
                    "fallback": "browser_speech",
                },
                deadline=deadline,
                generation_id=generation_id,
                cancel_event=cancel_event,
            )
            await self._emit_json(
                {
                    "type": "tts.browser",
                    "turn_id": turn_id,
                    "generation_id": generation_id,
                    "text": phrase,
                },
                deadline=deadline,
                generation_id=generation_id,
                cancel_event=cancel_event,
            )
        else:
            self._observe_elapsed("tts", tts_started_at, "success")
            if self.adapters.tts.browser_fallback:
                await self._emit_json(
                    {
                        "type": "tts.browser",
                        "turn_id": turn_id,
                        "generation_id": generation_id,
                        "text": phrase,
                    },
                    deadline=deadline,
                    generation_id=generation_id,
                    cancel_event=cancel_event,
                )
        finally:
            if tts_iterator is not None:
                await self._close_async_iterator(tts_iterator, "tts")
        if not self._is_current(generation_id, cancel_event):
            return
        await self._emit_json(
            {
                "type": "tts.phrase_end",
                "turn_id": turn_id,
                "generation_id": generation_id,
                "text": phrase,
            },
            deadline=deadline,
            generation_id=generation_id,
            cancel_event=cancel_event,
        )
        if not tts_failed:
            synchronized_media_started = await self._animate_phrase(
                phrase,
                bytes(audio),
                sample_rate or 16_000,
                turn_id,
                generation_id,
                cancel_event,
                deadline,
                audio_frames if synchronized_avatar else None,
            )
            if (
                synchronized_avatar
                and not synchronized_media_started
                and self._is_current(generation_id, cancel_event)
            ):
                for frame in audio_frames:
                    await self._emit_binary(
                        pack_packet(
                            PacketKind.TTS_PCM16,
                            turn_id,
                            frame.pts_ms,
                            frame.pcm,
                        ),
                        deadline=deadline,
                        generation_id=generation_id,
                        cancel_event=cancel_event,
                    )

    async def _animate_phrase(
        self,
        phrase: str,
        audio: bytes,
        sample_rate: int,
        turn_id: int,
        generation_id: int,
        cancel_event: asyncio.Event,
        deadline: float,
        synchronized_audio: list[AudioFrame] | None = None,
    ) -> bool:
        synchronized_media_started = False
        video_pts_ms = 0
        avatar_started_at = time.perf_counter()
        avatar_iterator: AsyncIterator[Any] | None = None
        try:
            self._authorize()
            avatar_iterator = aiter(
                self.adapters.avatar.animate(
                    phrase, audio, sample_rate, self.persona, cancel_event
                )
            )
            phrase_deadline = min(
                deadline, time.monotonic() + self.avatar_phrase_timeout
            )
            while True:
                try:
                    segment = await self._await_bounded(
                        anext(avatar_iterator),
                        self._stage_timeout(phrase_deadline, None, "avatar"),
                        "avatar",
                    )
                except StopAsyncIteration:
                    break
                if not self._is_current(generation_id, cancel_event):
                    return False
                self._authorize()
                await self._emit_json(
                    {
                        "type": "avatar.segment",
                        "turn_id": turn_id,
                        "generation_id": generation_id,
                        "kind": segment.kind,
                        "index": segment.index,
                        "url": segment.url,
                        "mime_type": segment.mime_type,
                        "duration_ms": segment.duration_ms,
                        "metadata": segment.metadata,
                    },
                    deadline=deadline,
                    generation_id=generation_id,
                    cancel_event=cancel_event,
                )
                if segment.data:
                    if (
                        synchronized_audio is not None
                        and not synchronized_media_started
                    ):
                        if not synchronized_audio:
                            raise RuntimeError(
                                "synchronized avatar has no audio frames"
                            )
                        await self._emit_json(
                            {
                                "type": "av.sync_begin",
                                "turn_id": turn_id,
                                "generation_id": generation_id,
                                "sample_rate": synchronized_audio[0].sample_rate,
                                "channels": synchronized_audio[0].channels,
                                "codec": synchronized_audio[0].codec,
                            },
                            deadline=deadline,
                            generation_id=generation_id,
                            cancel_event=cancel_event,
                        )
                        for frame in synchronized_audio:
                            await self._emit_binary(
                                pack_packet(
                                    PacketKind.TTS_PCM16,
                                    turn_id,
                                    frame.pts_ms,
                                    frame.pcm,
                                ),
                                deadline=deadline,
                                generation_id=generation_id,
                                cancel_event=cancel_event,
                            )
                    await self._emit_binary(
                        pack_packet(
                            PacketKind.VIDEO_FRAGMENT,
                            turn_id,
                            video_pts_ms,
                            segment.data,
                        ),
                        deadline=deadline,
                        generation_id=generation_id,
                        cancel_event=cancel_event,
                    )
                    synchronized_media_started = True
                    video_pts_ms += max(0, segment.duration_ms)
            self._observe_elapsed("avatar", avatar_started_at, "success")
            return synchronized_media_started or synchronized_audio is None
        except AuthorizationFailure:
            raise
        except SlowConsumerError:
            raise
        except Exception as exc:  # noqa: BLE001 - avatar failure triggers safe fallback
            self._observe_elapsed(
                "avatar",
                avatar_started_at,
                "timeout" if isinstance(exc, StageTimeout) else "error",
            )
            reference = self._log_exception("avatar", exc)
            await self._emit_json(
                {
                    "type": "degraded",
                    "component": "avatar",
                    "reason": f"internal error ({reference})",
                    "fallback": "static_avatar",
                },
                deadline=deadline,
                generation_id=generation_id,
                cancel_event=cancel_event,
            )
            return synchronized_media_started
        finally:
            if avatar_iterator is not None:
                await self._close_async_iterator(avatar_iterator, "avatar")

    async def cancel_response(self, reason: str = "cancelled") -> None:
        async with self._turn_lock:
            if self.state is SessionState.CLOSED:
                return
            await self._cancel_active_locked(reason)
            if reason == "client_cancelled":
                await self._set_state(SessionState.LISTENING)

    async def _cancel_active_locked(self, reason: str) -> None:
        """Invalidate and stop the active generation while ``_turn_lock`` is held."""

        task = self._response_task
        had_prior_generation = self.generation_id > 0 or task is not None
        old_generation_id = self.generation_id
        self._cancel_event.set()
        if had_prior_generation:
            self._increment_metric("cancellation", "requested")
            self.generation_id += 1
        self._response_task = None
        cancellation_timed_out = False
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                done, _ = await asyncio.wait({task}, timeout=self.cancellation_timeout)
            except asyncio.CancelledError:
                self._retire_task(task, "response_cancel")
                raise
            if not done:
                cancellation_timed_out = True
                self._retire_task(task, "response_cancel")
        if had_prior_generation:
            await self._emit_json(
                {
                    "type": "playout.clear",
                    "reason": reason,
                    "generation_id": old_generation_id,
                }
            )
        if cancellation_timed_out:
            await self._emit_json(
                {
                    "type": "degraded",
                    "component": "turn_cancel",
                    "reason": "adapter did not stop within the cancellation budget",
                    "fallback": "generation_quarantine",
                }
            )

    async def close(self) -> None:
        shutdown_task = self._shutdown_task
        if shutdown_task is None:
            shutdown_task = asyncio.create_task(
                self._close_impl(),
                name=f"echoweave-shutdown-{self.session_id}",
            )
            self._shutdown_task = shutdown_task
        try:
            await asyncio.shield(shutdown_task)
        except asyncio.CancelledError:
            # A disconnect must not interrupt model/client cleanup halfway through.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(shutdown_task)
            raise

    async def _close_impl(self) -> None:
        primary_error: Exception | None = None
        try:
            async with self._turn_lock:
                if self.state is not SessionState.CLOSED:
                    await self._cancel_active_locked("session_closed")
                    self._speech_active = False
                    self._utterance.clear()
                    self._pre_roll.clear()
                    with contextlib.suppress(Exception):
                        self.adapters.vad.reset()
                    await self._set_state(SessionState.CLOSED)
        except Exception as exc:  # noqa: BLE001 - cleanup must run after close failures
            primary_error = exc
            self._cancel_event.set()
            async with self._state_lock:
                self.state = SessionState.CLOSED
        finally:
            try:
                await self._cancel_retired_tasks()
            finally:
                await self._close_adapters_once()
        if primary_error is not None:
            raise primary_error

    async def wait_idle(self, timeout: float = 10.0) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be positive")
        task = self._response_task
        if task and not task.done():
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if not done:
                raise TimeoutError("session did not become idle before the timeout")
        if task and task.done() and not task.cancelled():
            task.result()

    @property
    def pending_task_count(self) -> int:
        """Number of active or quarantined tasks, useful for health diagnostics."""

        active = int(self._response_task is not None and not self._response_task.done())
        return active + sum(not task.done() for task in self._retired_tasks)

    def _is_current(self, generation_id: int, cancel_event: asyncio.Event) -> bool:
        return (
            self.state is not SessionState.CLOSED
            and generation_id == self.generation_id
            and not cancel_event.is_set()
        )

    def _response_done(self, task: asyncio.Task[None]) -> None:
        if self._response_task is task:
            self._response_task = None
        if task.cancelled():
            return
        with contextlib.suppress(Exception):
            task.result()

    def _retire_task(self, task: asyncio.Future[Any], component: str) -> None:
        if task.done():
            self._consume_retired_task(task, component)
            return
        self._retired_tasks.add(task)

        def done(completed: asyncio.Future[Any]) -> None:
            self._retired_tasks.discard(completed)
            self._consume_retired_task(completed, component)

        task.add_done_callback(done)

    def _consume_retired_task(self, task: asyncio.Future[Any], component: str) -> None:
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except (asyncio.CancelledError, asyncio.InvalidStateError):
            return
        if exc is not None:
            self._log_exception(component, exc)

    async def _await_bounded(
        self,
        awaitable: Awaitable[Any],
        timeout: float,
        stage: str,
    ) -> Any:
        task = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()
            self._retire_task(task, stage)
            raise
        if not done:
            task.cancel()
            self._retire_task(task, stage)
            raise StageTimeout(stage)
        return task.result()

    async def _await_existing_task(
        self,
        task: asyncio.Task[Any],
        timeout: float,
        stage: str,
    ) -> Any:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            task.cancel()
            self._retire_task(task, stage)
            raise StageTimeout(stage)
        return task.result()

    def _stage_timeout(
        self,
        deadline: float,
        stage_cap: float | None,
        stage: str,
    ) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StageTimeout(stage)
        return remaining if stage_cap is None else min(remaining, stage_cap)

    async def _queue_speech(
        self,
        queue: asyncio.Queue[str | object],
        item: str | object,
        speech_task: asyncio.Task[None],
        deadline: float,
        generation_id: int,
        cancel_event: asyncio.Event,
    ) -> None:
        if not self._is_current(generation_id, cancel_event):
            return
        timeout = self._stage_timeout(
            deadline,
            self.speech_backpressure_timeout,
            "speech_backpressure",
        )
        if queue.full():
            self._increment_metric("backpressure", "wait")
        put_task = asyncio.create_task(queue.put(item))
        try:
            done, _ = await asyncio.wait(
                {put_task, speech_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            put_task.cancel()
            self._retire_task(put_task, "speech_queue_put")
            raise
        if speech_task in done:
            if not put_task.done():
                put_task.cancel()
                self._retire_task(put_task, "speech_queue_put")
            if speech_task.cancelled():
                raise asyncio.CancelledError
            exception = speech_task.exception()
            if exception is not None:
                raise exception
            raise RuntimeError("speech worker stopped before the queue was drained")
        if put_task in done:
            put_task.result()
            return
        put_task.cancel()
        self._retire_task(put_task, "speech_queue_put")
        self._increment_metric("backpressure", "timeout")
        raise StageTimeout("speech_backpressure")

    async def _stop_task(self, task: asyncio.Task[Any], component: str) -> None:
        if task.done():
            if not task.cancelled():
                with contextlib.suppress(Exception):
                    task.result()
            return
        task.cancel()
        try:
            done, _ = await asyncio.wait({task}, timeout=self.cancellation_timeout)
        except asyncio.CancelledError:
            self._retire_task(task, component)
            raise
        if not done:
            self._retire_task(task, component)

    async def _cancel_retired_tasks(self) -> None:
        deadline = time.monotonic() + self.cancellation_timeout
        while True:
            tasks = {
                task
                for task in self._retired_tasks
                if not task.done() and task is not asyncio.current_task()
            }
            if not tasks:
                return
            for task in tasks:
                task.cancel()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._increment_metric("task_cleanup", "timeout")
                return
            _, pending = await asyncio.wait(tasks, timeout=remaining)
            if pending:
                self._increment_metric("task_cleanup", "timeout")
                return
            # Completion callbacks can register nested iterator/worker cleanup.
            await asyncio.sleep(0)

    async def _close_async_iterator(
        self, iterator: AsyncIterator[Any], component: str
    ) -> None:
        closer = getattr(iterator, "aclose", None)
        if closer is None:
            return
        try:
            result = closer()
            if inspect.isawaitable(result):
                await self._await_bounded(
                    result,
                    min(self.cancellation_timeout, 1.0),
                    f"{component}_close",
                )
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception as exc:  # noqa: BLE001 - iterator cleanup boundary
            self._log_exception(f"{component}_close", exc)

    async def _emit_json(
        self,
        payload: dict[str, Any],
        *,
        deadline: float | None = None,
        generation_id: int | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> bool:
        if generation_id is not None and (
            cancel_event is None or not self._is_current(generation_id, cancel_event)
        ):
            return False
        timeout = self.emit_timeout
        if deadline is not None:
            timeout = self._stage_timeout(deadline, timeout, "outbound_json")
        try:
            await self._await_bounded(self.emit_json(payload), timeout, "outbound_json")
        except StageTimeout as exc:
            raise SlowConsumerError(
                "JSON consumer exceeded its delivery budget"
            ) from exc
        return True

    async def _emit_binary(
        self,
        payload: bytes,
        *,
        deadline: float | None = None,
        generation_id: int | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> bool:
        if generation_id is not None and (
            cancel_event is None or not self._is_current(generation_id, cancel_event)
        ):
            return False
        timeout = self.emit_timeout
        if deadline is not None:
            timeout = self._stage_timeout(deadline, timeout, "outbound_binary")
        try:
            await self._await_bounded(
                self.emit_binary(payload), timeout, "outbound_binary"
            )
        except StageTimeout as exc:
            raise SlowConsumerError(
                "binary consumer exceeded its delivery budget"
            ) from exc
        return True

    async def _set_state(
        self,
        state: SessionState,
        turn_id: int | None = None,
        *,
        generation_id: int | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> bool:
        async with self._state_lock:
            if generation_id is not None and (
                cancel_event is None
                or not self._is_current(generation_id, cancel_event)
            ):
                return False
            if self.state is SessionState.CLOSED and state is not SessionState.CLOSED:
                return False
            if state not in _ALLOWED_STATE_TRANSITIONS[self.state]:
                raise RuntimeError(
                    f"invalid session state transition: {self.state.value} -> {state.value}"
                )
            self.state = state
            return await self._emit_json(
                {
                    "type": "session.state",
                    "state": state.value,
                    "turn_id": turn_id if turn_id is not None else self.turn_id,
                },
                generation_id=generation_id,
                cancel_event=cancel_event,
            )

    async def _error(
        self,
        code: str,
        message: str,
        turn_id: int | None = None,
        *,
        deadline: float | None = None,
    ) -> None:
        await self._emit_json(
            {
                "type": "error",
                "code": code,
                "message": message,
                "turn_id": turn_id if turn_id is not None else self.turn_id,
            },
            deadline=deadline,
        )

    async def _abort_generation(
        self,
        generation_id: int,
        cancel_event: asyncio.Event,
        reason: str,
        *,
        error_code: str,
        error_message: str,
        turn_id: int,
    ) -> None:
        async with self._turn_lock:
            if not self._is_current(generation_id, cancel_event):
                return
            cancel_event.set()
            self.generation_id += 1
            if self._response_task is asyncio.current_task():
                self._response_task = None
            await self._emit_json(
                {
                    "type": "playout.clear",
                    "reason": reason,
                    "generation_id": generation_id,
                }
            )
            await self._error(error_code, error_message, turn_id)
            if self.state is not SessionState.CLOSED:
                await self._set_state(SessionState.LISTENING, turn_id)

    async def _handle_authorization_failure(
        self,
        turn_id: int,
        generation_id: int,
        cancel_event: asyncio.Event,
    ) -> None:
        await self._abort_generation(
            generation_id,
            cancel_event,
            "authorization_revoked",
            error_code="authorization_revoked",
            error_message="persona authorization is no longer valid",
            turn_id=turn_id,
        )

    async def _handle_stage_timeout(
        self,
        stage: str,
        turn_id: int,
        generation_id: int,
        cancel_event: asyncio.Event,
    ) -> None:
        await self._abort_generation(
            generation_id,
            cancel_event,
            "latency_budget_exceeded",
            error_code="latency_budget_exceeded",
            error_message=f"realtime stage exceeded its latency budget: {stage}",
            turn_id=turn_id,
        )

    async def _quarantine_slow_consumer(
        self, generation_id: int, cancel_event: asyncio.Event
    ) -> None:
        async with self._turn_lock:
            if generation_id != self.generation_id:
                return
            cancel_event.set()
            self.generation_id += 1
            if self._response_task is asyncio.current_task():
                self._response_task = None
            async with self._state_lock:
                self.state = SessionState.CLOSED

    async def _close_adapters_once(self) -> None:
        async with self._adapter_close_lock:
            if self._adapters_closed:
                return
            self._adapters_closed = True
            unique: list[tuple[str, Any]] = []
            seen: set[int] = set()
            for name in ("vad", "asr", "llm", "tts", "avatar"):
                adapter = getattr(self.adapters, name, None)
                if adapter is None or id(adapter) in seen:
                    continue
                seen.add(id(adapter))
                if callable(getattr(adapter, "aclose", None)) or callable(
                    getattr(adapter, "close", None)
                ):
                    unique.append((name, adapter))

            async def close_one(name: str, adapter: Any) -> None:
                try:
                    async_close = getattr(adapter, "aclose", None)
                    if callable(async_close):
                        result = async_close()
                        if not inspect.isawaitable(result):
                            raise TypeError("adapter aclose() must return an awaitable")
                    else:
                        sync_close = getattr(adapter, "close", None)
                        if not callable(sync_close):
                            return
                        result = await asyncio.to_thread(sync_close)
                    if inspect.isawaitable(result):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - adapter cleanup boundary
                    self._log_exception(f"{name}_close", exc)

            tasks = {
                asyncio.create_task(
                    close_one(name, adapter),
                    name=f"echoweave-close-{name}-{self.session_id}",
                )
                for name, adapter in unique
            }
            if not tasks:
                return
            _, pending = await asyncio.wait(
                tasks,
                timeout=self.adapter_close_timeout,
            )
            for task in pending:
                task.cancel()
                self._retire_task(task, "adapter_close")

    def _observe_elapsed(
        self,
        component: str,
        started_at: float,
        outcome: str,
        *,
        monotonic: bool = False,
    ) -> None:
        if self.metrics is None:
            return
        clock = time.monotonic if monotonic else time.perf_counter
        try:
            self.metrics.observe_latency(
                "echoweave.stage_latency",
                max(0.0, (clock() - started_at) * 1_000),
                labels={"component": component, "outcome": outcome},
            )
        except Exception as exc:  # noqa: BLE001 - metrics cannot break the media path
            LOGGER.warning("metrics observation failed: %s", type(exc).__name__)

    def _increment_metric(self, component: str, outcome: str) -> None:
        if self.metrics is None:
            return
        try:
            self.metrics.increment(
                "echoweave.events",
                labels={"component": component, "outcome": outcome},
            )
        except Exception as exc:  # noqa: BLE001 - metrics cannot break the media path
            LOGGER.warning("metrics increment failed: %s", type(exc).__name__)

    def _authorize(self, *, force: bool = False) -> None:
        if self.authorization_check is not None:
            now = time.monotonic()
            if (
                not force
                and now - self._last_authorization_check
                < self.authorization_check_interval
            ):
                return
            try:
                self.authorization_check()
            except Exception as exc:
                raise AuthorizationFailure from exc
            self._last_authorization_check = now

    @staticmethod
    def _log_exception(component: str, exc: Exception) -> str:
        reference = uuid.uuid4().hex[:12]
        LOGGER.error(
            "%s failure reference=%s: %s",
            component,
            reference,
            type(exc).__name__,
            exc_info=exc,
        )
        return reference

    async def _internal_error(
        self, code: str, exc: Exception, turn_id: int | None = None
    ) -> None:
        reference = self._log_exception(code, exc)
        await self._error(
            code,
            f"component failed; reference {reference}",
            turn_id,
        )
