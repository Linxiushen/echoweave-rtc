import asyncio
import base64
import json

import httpx
import pytest

from echoweave.adapters.asr import Qwen3ASRHTTP
from echoweave.adapters.avatar import SoulXHTTPAvatar
from echoweave.adapters.http import ManagedAsyncClient
from echoweave.adapters.llm import DeepSeekV4Flash, OpenAICompatibleLLM
from echoweave.adapters.tts import VoxCPM2HTTP
from echoweave.contracts import PersonaProfile


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.iterated = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.iterated += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _persona(*, with_image: bool = False) -> PersonaProfile:
    persona = PersonaProfile(
        persona_id="demo",
        display_name="Demo",
        system_prompt="system",
        disclosure_text="AI",
        is_fictional=True,
    )
    if with_image:
        persona.reference_image_data = b"not-a-real-png"
        persona.reference_image_name = "avatar.png"
    return persona


async def test_managed_http_client_reuses_pool_closes_once_and_ignores_env():
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True}, request=request)

    owner = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    assert owner._trust_env is False
    first = await owner.get()
    second = await owner.get()
    assert first is second
    assert (await first.get("https://worker.test/health")).json() == {"ok": True}
    assert calls == 1

    await owner.aclose()
    await owner.aclose()
    with pytest.raises(RuntimeError, match="already been closed"):
        await owner.get()


async def test_qwen_streams_fragmented_json_and_reuses_owned_client():
    streams: list[ChunkStream] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        stream = ChunkStream([b'{"te', b'xt":"hello",', b'"language":"English"}'])
        streams.append(stream)
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            stream=stream,
            request=request,
        )

    adapter = Qwen3ASRHTTP("https://worker.test/v1")
    adapter._http = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    first = await adapter.transcribe(b"\x00\x00" * 32, 16_000)
    second = await adapter.transcribe(b"\x00\x00" * 32, 16_000)
    assert first.text == second.text == "hello"
    assert all(stream.closed for stream in streams)

    await adapter.aclose()
    with pytest.raises(RuntimeError, match="already been closed"):
        await adapter.transcribe(b"\x00\x00" * 32, 16_000)


@pytest.mark.parametrize(
    ("headers", "chunks", "error"),
    [
        (
            {"content-type": "application/json", "content-length": "65"},
            [b"{}"],
            "announced an oversized response",
        ),
        (
            {"content-type": "application/json", "content-length": "2"},
            [b'{"text":"hello"}'],
            "did not match its Content-Length",
        ),
        (
            {"content-type": "application/json"},
            [b"x" * 65],
            "exceeded the response size limit",
        ),
        (
            {"content-type": "text/plain"},
            [b"{}"],
            "unsupported Content-Type",
        ),
        ({}, [b"{}"], "omitted the Content-Type"),
        (
            {
                "content-type": "application/json",
                "content-encoding": "gzip",
            },
            [b"{}"],
            "unsupported Content-Encoding",
        ),
    ],
)
async def test_qwen_rejects_untrusted_response_framing(
    headers: dict[str, str],
    chunks: list[bytes],
    error: str,
):
    stream = ChunkStream(chunks)

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=headers,
            stream=stream,
            request=request,
        )

    adapter = Qwen3ASRHTTP(
        "https://worker.test/v1",
        max_response_bytes=64,
    )
    adapter._http = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    try:
        with pytest.raises(RuntimeError, match=error):
            await adapter.transcribe(b"\x00\x00", 16_000)
    finally:
        await adapter.aclose()
    assert stream.closed
    if (
        "announced" in error
        or "unsupported Content-" in error
        or "omitted the Content-Type" in error
    ):
        assert stream.iterated == 0


async def test_deepseek_streams_sse_split_across_arbitrary_raw_chunks():
    stream = ChunkStream(
        [
            b'data: {"choices":[{"delta":',
            b'{"content":"hel"}}]}\r',
            b'\n\r\ndata: {"choices":[{"delta":{"content":"lo"}}]}\n',
            b"\ndata: [DO",
            b"NE]\n\n",
        ]
    )

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            stream=stream,
            request=request,
        )

    adapter = DeepSeekV4Flash("test-key")
    adapter._http = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    try:
        chunks = [
            chunk
            async for chunk in adapter.stream(
                [{"role": "user", "content": "hello"}],
                asyncio.Event(),
            )
        ]
    finally:
        await adapter.aclose()
    assert chunks == ["hel", "lo"]
    assert stream.closed


async def test_openai_compatible_stream_uses_standard_body_without_vendor_fields():
    captured: dict[str, object] = {}

    async def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkStream(
                [
                    b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n',
                    b"data: [DONE]\n\n",
                ]
            ),
            request=request,
        )

    adapter = OpenAICompatibleLLM(
        "test-key",
        "https://gateway.test/v1/",
        "fast-model",
        max_tokens=321,
    )
    adapter._http = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    try:
        chunks = [
            chunk
            async for chunk in adapter.stream(
                [{"role": "user", "content": "hello"}],
                asyncio.Event(),
            )
        ]
    finally:
        await adapter.aclose()

    assert chunks == ["OK"]
    assert captured == {
        "url": "https://gateway.test/v1/chat/completions",
        "authorization": "Bearer test-key",
        "body": {
            "model": "fast-model",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "max_tokens": 321,
        },
    }


async def test_openai_compatible_aggregates_multiline_sse_data_events():
    stream = ChunkStream(
        [
            b"event: message\n",
            b'data: {"choices":[{"delta":\n',
            b'data: {"content":"hello"},"finish_reason":"stop"}]}\n\n',
        ]
    )

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
            request=request,
        )

    adapter = OpenAICompatibleLLM(
        "test-key",
        "https://gateway.test/v1",
        "fast-model",
    )
    adapter._http = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    try:
        chunks = [
            chunk
            async for chunk in adapter.stream(
                [{"role": "user", "content": "hello"}],
                asyncio.Event(),
            )
        ]
    finally:
        await adapter.aclose()

    assert chunks == ["hello"]
    assert stream.closed


@pytest.mark.parametrize(
    "body",
    [
        b'event: error\ndata: {"message":"private provider detail"}\n\n',
        b'data: {"error":{"message":"private provider detail"}}\n\n',
    ],
)
async def test_openai_compatible_rejects_sse_error_events_without_leaking_details(
    body: bytes,
):
    stream = ChunkStream([body])

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
            request=request,
        )

    adapter = OpenAICompatibleLLM(
        "test-key",
        "https://gateway.test/v1",
        "fast-model",
    )
    adapter._http = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    try:
        with pytest.raises(RuntimeError, match="streamed error") as caught:
            _ = [
                chunk
                async for chunk in adapter.stream(
                    [{"role": "user", "content": "hello"}],
                    asyncio.Event(),
                )
            ]
    finally:
        await adapter.aclose()

    assert "private provider detail" not in str(caught.value)
    assert stream.closed


async def test_openai_compatible_rejects_eof_without_a_terminal_event():
    stream = ChunkStream([b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'])

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
            request=request,
        )

    adapter = OpenAICompatibleLLM(
        "test-key",
        "https://gateway.test/v1",
        "fast-model",
    )
    adapter._http = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    iterator = adapter.stream([], asyncio.Event())
    try:
        assert await anext(iterator) == "partial"
        with pytest.raises(RuntimeError, match="before a terminal event"):
            await anext(iterator)
    finally:
        await iterator.aclose()
        await adapter.aclose()

    assert stream.closed


async def test_openai_compatible_rejects_error_after_partial_content():
    stream = ChunkStream(
        [
            b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
            b'data: {"error":{"message":"upstream failed"}}\n\n',
        ]
    )

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
            request=request,
        )

    adapter = OpenAICompatibleLLM(
        "test-key",
        "https://gateway.test/v1",
        "fast-model",
    )
    adapter._http = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    iterator = adapter.stream([], asyncio.Event())
    try:
        assert await anext(iterator) == "partial"
        with pytest.raises(RuntimeError, match="streamed error"):
            await anext(iterator)
    finally:
        await iterator.aclose()
        await adapter.aclose()

    assert stream.closed


async def test_openai_compatible_rejects_incomplete_finish_reasons():
    stream = ChunkStream(
        [
            (
                b'data: {"choices":[{"delta":{"content":"cut"},'
                b'"finish_reason":"length"}]}\n\ndata: [DONE]\n\n'
            )
        ]
    )

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
            request=request,
        )

    adapter = OpenAICompatibleLLM(
        "test-key",
        "https://gateway.test/v1",
        "fast-model",
    )
    adapter._http = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    try:
        with pytest.raises(RuntimeError, match="incomplete or unsupported"):
            _ = [chunk async for chunk in adapter.stream([], asyncio.Event())]
    finally:
        await adapter.aclose()

    assert stream.closed


async def test_deepseek_retains_explicit_thinking_control():
    captured_body: dict[str, object] = {}

    async def respond(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=ChunkStream([b"data: [DONE]\n\n"]),
            request=request,
        )

    adapter = DeepSeekV4Flash("test-key", thinking="disabled")
    adapter._http = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    try:
        assert [
            chunk
            async for chunk in adapter.stream(
                [{"role": "user", "content": "hello"}],
                asyncio.Event(),
            )
        ] == []
    finally:
        await adapter.aclose()

    assert captured_body["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize(
    ("chunks", "line_limit", "total_limit", "error"),
    [
        ([b"x" * 9, b"x" * 9], 16, 128, "oversized line"),
        ([b"x" * 128], 16, 256, "oversized line"),
        ([b": comment\n" * 8], 16, 32, "response size limit"),
        ([b"data: 123456789\n" * 3 + b"\n"], 16, 128, "oversized SSE event"),
    ],
)
async def test_deepseek_rejects_unbounded_raw_streams(
    chunks: list[bytes],
    line_limit: int,
    total_limit: int,
    error: str,
):
    stream = ChunkStream(chunks)

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
            request=request,
        )

    adapter = DeepSeekV4Flash(
        "test-key",
        max_sse_line_chars=line_limit,
        max_response_bytes=total_limit,
    )
    adapter._http = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    try:
        with pytest.raises(RuntimeError, match=error):
            _ = [
                chunk
                async for chunk in adapter.stream(
                    [{"role": "user", "content": "hello"}],
                    asyncio.Event(),
                )
            ]
    finally:
        await adapter.aclose()
    assert stream.closed


async def test_deepseek_stream_enforces_total_output_limit_and_content_type():
    responses = [
        (
            {"content-type": "text/event-stream"},
            [(b'data: {"choices":[{"delta":{"content":"abcd"}}]}\n\ndata: [DONE]\n\n')],
        ),
        ({"content-type": "application/json"}, []),
    ]

    async def respond(request: httpx.Request) -> httpx.Response:
        headers, chunks = responses.pop(0)
        return httpx.Response(
            200,
            headers=headers,
            stream=ChunkStream(chunks),
            request=request,
        )

    adapter = DeepSeekV4Flash("test-key", max_output_chars=3)
    adapter._http = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    try:
        for expected in ("safety limit", "Content-Type"):
            with pytest.raises(RuntimeError, match=expected):
                _ = [
                    chunk
                    async for chunk in adapter.stream(
                        [{"role": "user", "content": "hello"}],
                        asyncio.Event(),
                    )
                ]
    finally:
        await adapter.aclose()


async def test_deepseek_cancellation_closes_the_stream():
    cancelled_stream = ChunkStream(
        [
            b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"two"}}]}\n\n',
        ]
    )
    reused_stream = ChunkStream(
        [
            b'data: {"choices":[{"delta":{"content":"again"}}]}\n\n',
            b"data: [DONE]\n\n",
        ]
    )
    streams = [cancelled_stream, reused_stream]

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=streams.pop(0),
            request=request,
        )

    adapter = DeepSeekV4Flash("test-key")
    adapter._http = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    cancel_event = asyncio.Event()
    iterator = adapter.stream([], cancel_event)
    assert await anext(iterator) == "one"
    cancel_event.set()
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)
    assert cancelled_stream.closed

    assert [chunk async for chunk in adapter.stream([], asyncio.Event())] == ["again"]
    assert reused_stream.closed
    await adapter.aclose()


async def test_voxcpm_streams_fragmented_pcm_with_monotonic_timestamps():
    stream = ChunkStream([b"\x01", b"\x02\x03\x04", b"\x05\x06"])

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "audio/pcm; rate=48000",
                "x-audio-sample-rate": "48000",
            },
            stream=stream,
            request=request,
        )

    adapter = VoxCPM2HTTP("https://worker.test/v1")
    adapter._http = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    try:
        frames = [
            frame
            async for frame in adapter.synthesize(
                "hello",
                _persona(),
                asyncio.Event(),
            )
        ]
    finally:
        await adapter.aclose()
    assert b"".join(frame.pcm for frame in frames) == b"\x01\x02\x03\x04\x05\x06"
    assert [frame.pts_ms for frame in frames] == sorted(
        frame.pts_ms for frame in frames
    )
    assert stream.closed


@pytest.mark.parametrize(
    ("headers", "chunks", "error"),
    [
        (
            {"content-type": "audio/pcm", "content-length": "5"},
            [b"\x00\x00"],
            "announced an oversized response",
        ),
        (
            {"content-type": "audio/pcm", "content-length": "2"},
            [b"\x00\x00\x00\x00"],
            "did not match its Content-Length",
        ),
        (
            {"content-type": "audio/pcm"},
            [b"\x00" * 6],
            "exceeded the response size limit",
        ),
        (
            {"content-type": "audio/mpeg"},
            [b"\x00\x00"],
            "unsupported Content-Type",
        ),
    ],
)
async def test_voxcpm_rejects_untrusted_response_framing(
    headers: dict[str, str],
    chunks: list[bytes],
    error: str,
):
    stream = ChunkStream(chunks)

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=headers,
            stream=stream,
            request=request,
        )

    adapter = VoxCPM2HTTP(
        "https://worker.test/v1",
        sample_rate=2,
        max_output_seconds=1,
    )
    adapter._http = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    try:
        with pytest.raises(RuntimeError, match=error):
            _ = [
                frame
                async for frame in adapter.synthesize(
                    "hello",
                    _persona(),
                    asyncio.Event(),
                )
            ]
    finally:
        await adapter.aclose()
    assert stream.closed
    if "announced" in error or "Content-Type" in error:
        assert stream.iterated == 0


async def test_voxcpm_rejects_truncated_pcm_and_sample_rate_mismatch():
    responses = [
        ({"content-type": "audio/pcm"}, [b"\x00"]),
        (
            {
                "content-type": "audio/pcm",
                "x-audio-sample-rate": "24000",
            },
            [b"\x00\x00"],
        ),
    ]

    async def respond(request: httpx.Request) -> httpx.Response:
        headers, chunks = responses.pop(0)
        return httpx.Response(
            200,
            headers=headers,
            stream=ChunkStream(chunks),
            request=request,
        )

    adapter = VoxCPM2HTTP("https://worker.test/v1")
    adapter._http = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    try:
        for expected in ("truncated PCM16", "sample rate"):
            with pytest.raises(RuntimeError, match=expected):
                _ = [
                    frame
                    async for frame in adapter.synthesize(
                        "hello",
                        _persona(),
                        asyncio.Event(),
                    )
                ]
    finally:
        await adapter.aclose()


async def test_soulx_streams_fragmented_ndjson_segments():
    events = [
        {
            "index": 0,
            "duration_ms": 20,
            "mime_type": "video/mp4",
            "data_b64": base64.b64encode(b"one").decode(),
        },
        {
            "index": 1,
            "duration_ms": 30,
            "mime_type": "video/mp4",
            "data_b64": base64.b64encode(b"two").decode(),
        },
    ]
    body = b"".join(json.dumps(event).encode() + b"\n" for event in events)
    stream = ChunkStream([body[:7], body[7:31], body[31:]])

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/x-ndjson; charset=utf-8"},
            stream=stream,
            request=request,
        )

    adapter = SoulXHTTPAvatar("https://worker.test")
    adapter._http = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    try:
        segments = [
            segment
            async for segment in adapter.animate(
                "hello",
                b"\x00\x00",
                16_000,
                _persona(with_image=True),
                asyncio.Event(),
            )
        ]
    finally:
        await adapter.aclose()
    assert [segment.index for segment in segments] == [0, 1]
    assert [segment.data for segment in segments] == [b"one", b"two"]
    assert stream.closed


@pytest.mark.parametrize(
    ("headers", "chunks", "line_limit", "total_limit", "error"),
    [
        (
            {"content-type": "application/x-ndjson"},
            [b"x" * 9, b"x" * 9],
            16,
            128,
            "oversized line",
        ),
        (
            {"content-type": "application/x-ndjson"},
            [b"x" * 128],
            16,
            256,
            "oversized line",
        ),
        (
            {"content-type": "application/x-ndjson"},
            [b"{}\n" * 20],
            16,
            32,
            "response size limit",
        ),
        (
            {"content-type": "application/json"},
            [b"{}\n"],
            16,
            32,
            "unsupported Content-Type",
        ),
        (
            {
                "content-type": "application/x-ndjson",
                "content-length": "33",
            },
            [b"{}\n"],
            16,
            32,
            "announced an oversized response",
        ),
    ],
)
async def test_soulx_rejects_untrusted_or_unbounded_streams(
    headers: dict[str, str],
    chunks: list[bytes],
    line_limit: int,
    total_limit: int,
    error: str,
):
    stream = ChunkStream(chunks)

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=headers,
            stream=stream,
            request=request,
        )

    adapter = SoulXHTTPAvatar(
        "https://worker.test",
        max_segment_bytes=16,
        max_response_bytes=total_limit,
    )
    adapter.max_line_bytes = line_limit
    adapter._http = ManagedAsyncClient(
        5.0,
        transport=httpx.MockTransport(respond),
    )
    try:
        with pytest.raises(RuntimeError, match=error):
            _ = [
                segment
                async for segment in adapter.animate(
                    "hello",
                    b"\x00\x00",
                    16_000,
                    _persona(with_image=True),
                    asyncio.Event(),
                )
            ]
    finally:
        await adapter.aclose()
    assert stream.closed
    if "announced" in error or "Content-Type" in error:
        assert stream.iterated == 0
