from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from echoweave.adapters.http import (
    ManagedAsyncClient,
    iter_bounded_lines,
    require_content_type,
)


async def _iter_bounded_sse_events(
    response: httpx.Response,
    *,
    max_line_bytes: int,
    max_event_bytes: int,
    max_total_bytes: int,
    source: str,
) -> AsyncIterator[tuple[str, bytes]]:
    """Aggregate bounded SSE fields into complete data events."""

    data_lines: list[bytes] = []
    event_name = "message"
    event_bytes = 0

    def dispatch() -> tuple[str, bytes] | None:
        if not data_lines:
            return None
        return event_name, b"\n".join(data_lines)

    async for raw_line in iter_bounded_lines(
        response,
        max_line_bytes=max_line_bytes,
        max_total_bytes=max_total_bytes,
        source=source,
    ):
        if not raw_line:
            event = dispatch()
            if event is not None:
                yield event
            data_lines = []
            event_name = "message"
            event_bytes = 0
            continue

        event_bytes += len(raw_line) + 1
        if event_bytes > max_event_bytes:
            raise RuntimeError(f"{source} returned an oversized SSE event")
        if raw_line.startswith(b":"):
            continue

        field, separator, value = raw_line.partition(b":")
        if separator and value.startswith(b" "):
            value = value[1:]
        if field == b"data":
            data_lines.append(bytes(value))
        elif field == b"event":
            try:
                event_name = value.decode("utf-8", errors="strict") or "message"
            except UnicodeDecodeError as exc:
                raise RuntimeError(f"{source} returned invalid SSE text") from exc

    event = dispatch()
    if event is not None:
        yield event


class DemoLLM:
    async def stream(
        self,
        messages: list[dict[str, str]],
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[str]:
        user_text = next(
            (item["content"] for item in reversed(messages) if item["role"] == "user"),
            "",
        )
        reply = (
            "你好，我是 EchoWeave-RTC 的安全演示数字分身。"
            f"我收到了你的内容：“{user_text}”"
            "。当前是本地演示后端；配置 DeepSeek、VoxCPM2 和 SoulX 后，"
            "同一条实时链路会切换到完整模型。"
        )
        for index in range(0, len(reply), 5):
            if cancel_event.is_set():
                return
            await asyncio.sleep(0.015)
            yield reply[index : index + 5]


class OpenAICompatibleLLM:
    """Bounded OpenAI-compatible chat-completions SSE client."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        source: str = "OpenAI-compatible provider",
        max_tokens: int = 800,
        timeout_seconds: float = 120.0,
        max_output_chars: int = 64_000,
        max_sse_line_chars: int = 1024 * 1024,
        max_response_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if not api_key:
            raise ValueError("an API key is required for the OpenAI-compatible backend")
        if not base_url:
            raise ValueError("a base URL is required for the OpenAI-compatible backend")
        if not model:
            raise ValueError("a model is required for the OpenAI-compatible backend")
        if not source or len(source) > 80:
            raise ValueError("the OpenAI-compatible provider name is invalid")
        if not 1 <= max_tokens <= 16_384:
            raise ValueError("max_tokens is out of range")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.source = source
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars
        self.max_sse_line_chars = max_sse_line_chars
        self.max_response_bytes = max_response_bytes
        self._http = ManagedAsyncClient(
            httpx.Timeout(timeout_seconds, connect=15.0),
        )

    async def stream(
        self,
        messages: list[dict[str, str]],
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[str]:
        if cancel_event.is_set():
            return
        body = self._request_body(messages)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        client = await self._http.get()
        async with client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            headers=headers,
            json=body,
        ) as response:
            response.raise_for_status()
            require_content_type(
                response,
                {"text/event-stream"},
                source=self.source,
            )
            output_chars = 0
            finish_reason: str | None = None
            saw_done = False
            async for event_name, raw_data in _iter_bounded_sse_events(
                response,
                max_line_bytes=self.max_sse_line_chars,
                max_event_bytes=min(
                    self.max_response_bytes,
                    self.max_sse_line_chars * 2,
                ),
                max_total_bytes=self.max_response_bytes,
                source=self.source,
            ):
                if cancel_event.is_set():
                    return
                try:
                    data = raw_data.decode("utf-8", errors="strict").strip()
                except UnicodeDecodeError as exc:
                    raise RuntimeError(
                        f"{self.source} returned invalid SSE text"
                    ) from exc
                if event_name == "error":
                    raise RuntimeError(f"{self.source} returned a streamed error")
                if not data:
                    continue
                if data == "[DONE]":
                    saw_done = True
                    break
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"{self.source} returned invalid SSE JSON"
                    ) from exc
                if not isinstance(payload, dict):
                    raise TypeError(f"{self.source} returned an invalid SSE event")
                if "error" in payload:
                    raise RuntimeError(f"{self.source} returned a streamed error")
                choices = payload.get("choices")
                if choices is None:
                    continue
                if not isinstance(choices, list):
                    raise TypeError(f"{self.source} returned invalid streamed choices")
                if not choices:
                    continue
                if not isinstance(choices[0], dict):
                    raise TypeError(f"{self.source} returned invalid streamed choices")
                choice = choices[0]
                raw_finish_reason = choice.get("finish_reason")
                if raw_finish_reason is not None and (
                    not isinstance(raw_finish_reason, str) or not raw_finish_reason
                ):
                    raise RuntimeError(
                        f"{self.source} returned an invalid finish reason"
                    )
                if raw_finish_reason not in {None, "stop"}:
                    raise RuntimeError(
                        f"{self.source} returned an incomplete or unsupported completion"
                    )
                delta = choice.get("delta") or {}
                if not isinstance(delta, dict):
                    raise TypeError(f"{self.source} returned invalid streamed delta")
                content = delta.get("content")
                if content:
                    if not isinstance(content, str):
                        raise RuntimeError(
                            f"{self.source} returned invalid streamed content"
                        )
                    output_chars += len(content)
                    if output_chars > self.max_output_chars:
                        raise RuntimeError(
                            f"{self.source} output exceeded the safety limit"
                        )
                    if finish_reason is not None:
                        raise RuntimeError(
                            f"{self.source} returned content after completion"
                        )
                    yield content
                if raw_finish_reason is not None:
                    finish_reason = raw_finish_reason

            if cancel_event.is_set():
                return
            if not saw_done and finish_reason is None:
                raise RuntimeError(
                    f"{self.source} stream ended before a terminal event"
                )

    def _request_body(self, messages: list[dict[str, str]]) -> dict[str, object]:
        return {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "max_tokens": self.max_tokens,
        }

    async def aclose(self) -> None:
        await self._http.aclose()


class DeepSeekV4Flash(OpenAICompatibleLLM):
    """DeepSeek chat-completions client with its explicit thinking control."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
        thinking: str = "disabled",
        timeout_seconds: float = 120.0,
        max_output_chars: int = 64_000,
        max_sse_line_chars: int = 1024 * 1024,
        max_response_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY is required for the DeepSeek backend")
        self.thinking = thinking
        super().__init__(
            api_key,
            base_url,
            model,
            source="DeepSeek",
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
            max_sse_line_chars=max_sse_line_chars,
            max_response_bytes=max_response_bytes,
        )

    def _request_body(self, messages: list[dict[str, str]]) -> dict[str, object]:
        body = super()._request_body(messages)
        body["thinking"] = {"type": self.thinking}
        return body
