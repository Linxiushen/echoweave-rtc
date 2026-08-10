import asyncio

import pytest

from echoweave.adapters.asr import DemoASR
from echoweave.adapters.avatar import ClientLipSyncAvatar
from echoweave.adapters.llm import DemoLLM
from echoweave.adapters.tts import BrowserTTS
from echoweave.adapters.vad import EnergyVAD
from echoweave.contracts import AudioFrame, AvatarSegment, PersonaProfile, SessionState
from echoweave.observability import MetricRegistry
from echoweave.pipeline import RealtimeSession
from echoweave.protocol import PacketKind, unpack_packet
from echoweave.runtime import RuntimeAdapters


async def test_text_turn_runs_end_to_end():
    events = []
    binary = []

    async def emit_json(event):
        events.append(event)

    async def emit_binary(packet):
        binary.append(packet)

    adapters = RuntimeAdapters(
        EnergyVAD(), DemoASR(), DemoLLM(), BrowserTTS(), ClientLipSyncAvatar()
    )
    persona = PersonaProfile(
        persona_id="demo",
        display_name="Echo",
        system_prompt="You are a fictional AI.",
        disclosure_text="AI demo",
        is_fictional=True,
    )
    session = RealtimeSession("test", persona, adapters, emit_json, emit_binary)
    await session.start()
    await session.submit_text("测试一下")
    await session.wait_idle()

    event_types = [event["type"] for event in events]
    assert "assistant.delta" in event_types
    assert "assistant.final" in event_types
    assert "tts.browser" in event_types
    assert "avatar.segment" in event_types
    assert events[-1]["state"] == "listening"
    assert binary == []

    await session.cancel_response("client_cancelled")
    assert events[-2]["type"] == "playout.clear"
    assert events[-1] == {
        "type": "session.state",
        "state": "listening",
        "turn_id": 1,
    }


async def test_history_keeps_only_one_system_prompt():
    events = []

    async def emit_json(event):
        events.append(event)

    async def emit_binary(_packet):
        return None

    session = RealtimeSession(
        "history",
        PersonaProfile(
            persona_id="demo",
            display_name="Echo",
            system_prompt="system-once",
            disclosure_text="AI demo",
            is_fictional=True,
        ),
        RuntimeAdapters(
            EnergyVAD(),
            DemoASR(),
            DemoLLM(),
            BrowserTTS(),
            ClientLipSyncAvatar(),
        ),
        emit_json,
        emit_binary,
    )
    await session.start()
    await session.submit_text("first")
    await session.wait_idle()
    await session.submit_text("second")
    await session.wait_idle()

    assert [item["content"] for item in session._history].count("system-once") == 1


async def test_partial_tts_is_cleared_before_browser_fallback():
    outputs = []

    async def emit_json(event):
        outputs.append(("json", event))

    async def emit_binary(packet):
        outputs.append(("binary", packet))

    class PartialTTS:
        browser_fallback = False

        async def synthesize(self, _text, _persona, _cancel_event):
            yield AudioFrame(b"\x00\x00" * 480, 48_000)
            raise RuntimeError("synthetic failure")

    session = RealtimeSession(
        "partial-tts",
        PersonaProfile(
            persona_id="demo",
            display_name="Echo",
            system_prompt="system",
            disclosure_text="AI demo",
            is_fictional=True,
        ),
        RuntimeAdapters(
            EnergyVAD(),
            DemoASR(),
            DemoLLM(),
            PartialTTS(),
            ClientLipSyncAvatar(),
        ),
        emit_json,
        emit_binary,
    )
    await session.start()
    await session.submit_text("trigger")
    await session.wait_idle()

    json_events = [item for kind, item in outputs if kind == "json"]
    clear_indexes = [
        index
        for index, event in enumerate(json_events)
        if event["type"] == "playout.clear" and event.get("reason") == "tts_fallback"
    ]
    fallback_indexes = [
        index
        for index, event in enumerate(json_events)
        if event["type"] == "tts.browser"
    ]
    assert clear_indexes
    assert fallback_indexes
    assert clear_indexes[0] < fallback_indexes[0]
    assert not any(event["type"] == "avatar.segment" for event in json_events)


async def test_partial_llm_failure_aborts_generation_and_clears_playout():
    events = []
    packets = []
    media_sent = asyncio.Event()

    async def emit_json(event):
        events.append(event)

    async def emit_binary(packet):
        packets.append(packet)

    class PartialThenFailLLM:
        async def stream(self, _messages, _cancel_event):
            yield "This partial response is queued for speech before failure."
            await media_sent.wait()
            raise RuntimeError("stream ended without a terminal event")

    class BlockingTTS:
        browser_fallback = False

        def __init__(self):
            self.cancel_event = None

        async def synthesize(self, _text, _persona, cancel_event):
            self.cancel_event = cancel_event
            yield AudioFrame(b"\x00\x00" * 480, 48_000)
            media_sent.set()
            await cancel_event.wait()

    tts = BlockingTTS()
    session = RealtimeSession(
        "partial-llm-failure",
        _persona(),
        RuntimeAdapters(
            EnergyVAD(),
            DemoASR(),
            PartialThenFailLLM(),
            tts,
            ClientLipSyncAvatar(),
        ),
        emit_json,
        emit_binary,
    )
    await session.start()
    await session.submit_text("trigger")
    await session.wait_idle()
    await asyncio.sleep(0)

    clear = next(
        event
        for event in events
        if event["type"] == "playout.clear"
        and event.get("reason") == "generation_failed"
    )
    assert packets
    assert any(event["type"] == "assistant.delta" for event in events)
    assert not any(event["type"] == "assistant.final" for event in events)
    assert any(event.get("code") == "generation_failed" for event in events)
    assert tts.cancel_event is not None
    assert tts.cancel_event.is_set()
    assert session.generation_id > clear["generation_id"]
    assert session.state is SessionState.LISTENING
    assert session.pending_task_count == 0


async def test_authorization_failure_never_falls_back_to_browser_tts():
    events = []
    binary = []
    checks = 0

    async def emit_json(event):
        events.append(event)

    async def emit_binary(packet):
        binary.append(packet)

    def authorize():
        nonlocal checks
        checks += 1
        if checks >= 6:
            raise RuntimeError("revoked")

    class OnePhraseLLM:
        async def stream(self, _messages, _cancel_event):
            yield "这是一段用于授权撤回测试的完整回答。"

    class TwoFrameTTS:
        browser_fallback = False

        async def synthesize(self, _text, _persona, _cancel_event):
            yield AudioFrame(b"\x00\x00" * 480, 48_000)
            yield AudioFrame(b"\x00\x00" * 480, 48_000, pts_ms=10)

    session = RealtimeSession(
        "revoked",
        PersonaProfile(
            persona_id="demo",
            display_name="Echo",
            system_prompt="system",
            disclosure_text="AI demo",
            is_fictional=True,
        ),
        RuntimeAdapters(
            EnergyVAD(),
            DemoASR(),
            OnePhraseLLM(),
            TwoFrameTTS(),
            ClientLipSyncAvatar(),
        ),
        emit_json,
        emit_binary,
        authorization_check=authorize,
        authorization_check_interval=0,
    )
    await session.start()
    await session.submit_text("trigger")
    await session.wait_idle()

    assert binary
    assert not any(event["type"] == "tts.browser" for event in events)
    assert any(
        event["type"] == "playout.clear"
        and event.get("reason") == "authorization_revoked"
        for event in events
    )
    assert any(event.get("code") == "authorization_revoked" for event in events)


async def test_synchronized_avatar_failure_does_not_duplicate_audio():
    events = []
    packets = []

    async def emit_json(event):
        events.append(event)

    async def emit_binary(packet):
        packets.append(unpack_packet(packet))

    class OnePhraseLLM:
        async def stream(self, _messages, _cancel_event):
            yield "同步播放测试已经准备完成。"

    class TwoFrameTTS:
        browser_fallback = False

        async def synthesize(self, _text, _persona, _cancel_event):
            yield AudioFrame(b"\x00\x00" * 480, 48_000)
            yield AudioFrame(b"\x00\x00" * 480, 48_000, pts_ms=10)

    class OneThenFailAvatar:
        synchronized_playback = True

        async def animate(
            self,
            _text,
            _audio,
            _sample_rate,
            _persona,
            _cancel_event,
        ):
            yield AvatarSegment(
                kind="soulx_mp4",
                data=b"video",
                duration_ms=3_000,
            )
            raise RuntimeError("later segment failed")

    session = RealtimeSession(
        "sync",
        PersonaProfile(
            persona_id="demo",
            display_name="Echo",
            system_prompt="system",
            disclosure_text="AI demo",
            is_fictional=True,
        ),
        RuntimeAdapters(
            EnergyVAD(),
            DemoASR(),
            OnePhraseLLM(),
            TwoFrameTTS(),
            OneThenFailAvatar(),
        ),
        emit_json,
        emit_binary,
    )
    await session.start()
    await session.submit_text("trigger")
    await session.wait_idle()

    assert sum(packet.kind == PacketKind.TTS_PCM16 for packet in packets) == 2
    assert sum(packet.kind == PacketKind.VIDEO_FRAGMENT for packet in packets) == 1
    assert any(event["type"] == "av.sync_begin" for event in events)


def _persona():
    return PersonaProfile(
        persona_id="demo",
        display_name="Echo",
        system_prompt="system",
        disclosure_text="AI demo",
        is_fictional=True,
    )


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_session_rejects_non_finite_or_ambiguous_timeouts(timeout):
    async def emit_json(_event):
        return None

    async def emit_binary(_packet):
        return None

    with pytest.raises(ValueError, match="end_to_end_timeout must be positive"):
        RealtimeSession(
            "invalid-timeout",
            _persona(),
            RuntimeAdapters(
                EnergyVAD(),
                DemoASR(),
                DemoLLM(),
                BrowserTTS(),
                ClientLipSyncAvatar(),
            ),
            emit_json,
            emit_binary,
            end_to_end_timeout=timeout,
        )


async def test_bounded_speech_queue_aborts_when_tts_cannot_keep_up():
    events = []

    async def emit_json(event):
        events.append(event)

    async def emit_binary(_packet):
        return None

    class FastLLM:
        async def stream(self, _messages, _cancel_event):
            for index in range(8):
                yield f"这是第{index}条足够长的完整回答。"

    class SlowTTS:
        browser_fallback = False

        async def synthesize(self, _text, _persona, _cancel_event):
            await asyncio.sleep(1)
            yield AudioFrame(b"\x00\x00" * 480, 48_000)

    metrics = MetricRegistry()
    session = RealtimeSession(
        "backpressure",
        _persona(),
        RuntimeAdapters(
            EnergyVAD(),
            DemoASR(),
            FastLLM(),
            SlowTTS(),
            ClientLipSyncAvatar(),
        ),
        emit_json,
        emit_binary,
        speech_queue_size=1,
        speech_backpressure_timeout=0.02,
        cancellation_timeout=0.05,
        observability=metrics,
    )
    await session.start()
    await session.submit_text("trigger")
    await session.wait_idle()
    await asyncio.sleep(0)

    assert any(
        event.get("code") == "latency_budget_exceeded"
        and "speech_backpressure" in event["message"]
        for event in events
    )
    assert not any(event["type"] == "assistant.final" for event in events)
    assert session.state is SessionState.LISTENING
    assert session.pending_task_count == 0
    counters = metrics.snapshot()["counters"]
    assert any(
        item["name"] == "echoweave.events"
        and item["labels"] == {"component": "backpressure", "outcome": "timeout"}
        for item in counters
    )


async def test_new_generation_quarantines_cancellation_resistant_stream():
    events = []
    first_started = asyncio.Event()

    async def emit_json(event):
        events.append(event)

    async def emit_binary(_packet):
        return None

    class ResistantLLM:
        async def stream(self, messages, _cancel_event):
            text = next(
                item["content"] for item in reversed(messages) if item["role"] == "user"
            )
            if text == "first":
                first_started.set()
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.05)
                yield "stale response that must never escape。"
                return
            yield "fresh response is the only committed answer。"

    session = RealtimeSession(
        "epochs",
        _persona(),
        RuntimeAdapters(
            EnergyVAD(),
            DemoASR(),
            ResistantLLM(),
            BrowserTTS(),
            ClientLipSyncAvatar(),
        ),
        emit_json,
        emit_binary,
        cancellation_timeout=0.02,
    )
    await session.start()
    await session.submit_text("first")
    await asyncio.wait_for(first_started.wait(), 1)
    await session.submit_text("second")
    await session.wait_idle()
    await asyncio.sleep(0.08)

    finals = [event for event in events if event["type"] == "assistant.final"]
    assert len(finals) == 1
    assert finals[0]["text"] == "fresh response is the only committed answer。"
    assert not any(
        event.get("text") == "stale response that must never escape。"
        for event in events
    )
    assert session.pending_task_count == 0


async def test_slow_outbound_consumer_is_hard_bounded_and_quarantined():
    events = []

    async def emit_json(event):
        if event["type"] == "assistant.delta":
            await asyncio.sleep(1)
        events.append(event)

    async def emit_binary(_packet):
        return None

    session = RealtimeSession(
        "slow-consumer",
        _persona(),
        RuntimeAdapters(
            EnergyVAD(), DemoASR(), DemoLLM(), BrowserTTS(), ClientLipSyncAvatar()
        ),
        emit_json,
        emit_binary,
        emit_timeout=0.02,
        cancellation_timeout=0.05,
    )
    await session.start()
    await session.submit_text("trigger")
    await session.wait_idle()
    await asyncio.sleep(0)

    assert session.state is SessionState.CLOSED
    assert not any(event["type"] == "assistant.final" for event in events)
    assert session.pending_task_count == 0
    await session.close()


async def test_close_recancels_quarantined_adapter_tasks_before_resource_cleanup():
    started = asyncio.Event()
    first_cancel = asyncio.Event()
    second_cancel = asyncio.Event()

    class TwiceResistantLLM:
        async def stream(self, _messages, _cancel_event):
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                first_cancel.set()
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    second_cancel.set()
                    raise
            yield "stale"

    async def emit_json(_event):
        return None

    async def emit_binary(_packet):
        return None

    session = RealtimeSession(
        "retired-cleanup",
        _persona(),
        RuntimeAdapters(
            EnergyVAD(),
            DemoASR(),
            TwiceResistantLLM(),
            BrowserTTS(),
            ClientLipSyncAvatar(),
        ),
        emit_json,
        emit_binary,
        cancellation_timeout=0.02,
    )
    await session.start()
    await session.submit_text("first")
    await asyncio.wait_for(started.wait(), 1)
    await session.cancel_response("client_cancelled")
    await asyncio.wait_for(first_cancel.wait(), 1)

    await session.close()

    await asyncio.wait_for(second_cancel.wait(), 1)
    assert session.pending_task_count == 0


async def test_close_deduplicates_and_shields_async_adapter_cleanup():
    close_started = asyncio.Event()
    close_finished = asyncio.Event()

    class SharedAdapter:
        calls = 0

        async def aclose(self):
            self.calls += 1
            close_started.set()
            await asyncio.sleep(0.03)
            close_finished.set()

    shared = SharedAdapter()

    async def emit_json(_event):
        return None

    async def emit_binary(_packet):
        return None

    session = RealtimeSession(
        "adapter-close",
        _persona(),
        RuntimeAdapters(shared, shared, shared, shared, shared),
        emit_json,
        emit_binary,
        adapter_close_timeout=0.2,
    )
    close_task = asyncio.create_task(session.close())
    await asyncio.wait_for(close_started.wait(), 1)
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    assert close_finished.is_set()
    assert shared.calls == 1
    await session.close()
    assert shared.calls == 1


async def test_adapter_close_failure_isolated_from_other_adapters():
    closed = []

    class ClosingAdapter:
        def __init__(self, name, fail=False):
            self.name = name
            self.fail = fail

        async def aclose(self):
            closed.append(self.name)
            if self.fail:
                raise RuntimeError("synthetic close failure")

    async def emit_json(_event):
        return None

    async def emit_binary(_packet):
        return None

    adapters = [
        ClosingAdapter("vad"),
        ClosingAdapter("asr", fail=True),
        ClosingAdapter("llm"),
        ClosingAdapter("tts"),
        ClosingAdapter("avatar"),
    ]
    session = RealtimeSession(
        "adapter-close-isolation",
        _persona(),
        RuntimeAdapters(*adapters),
        emit_json,
        emit_binary,
    )
    await session.close()

    assert set(closed) == {"vad", "asr", "llm", "tts", "avatar"}
    assert session.state is SessionState.CLOSED


async def test_session_cleanup_supports_deduplicated_sync_close():
    class SyncAdapter:
        def __init__(self):
            self.close_count = 0

        def close(self):
            self.close_count += 1

    async def emit_json(_event):
        return None

    async def emit_binary(_packet):
        return None

    shared = SyncAdapter()
    session = RealtimeSession(
        "sync-adapter-close",
        _persona(),
        RuntimeAdapters(shared, shared, shared, shared, shared),
        emit_json,
        emit_binary,
    )

    await session.close()
    await session.close()

    assert shared.close_count == 1
