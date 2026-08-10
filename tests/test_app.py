import asyncio
import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient as _BaseTestClient
from starlette.websockets import WebSocketDisconnect

from echoweave.app import MAX_MIC_PAYLOAD_BYTES, create_app
from echoweave.auth import issue_session_token
from echoweave.config import Settings
from echoweave.persona import sign_manifest
from echoweave.protocol import PacketKind, pack_packet
from echoweave.runtime import RuntimeUnavailable

TEST_ACCESS_TOKEN = "test-access-token-with-at-least-32-bytes"
TEST_SIGNING_KEY = "test-signing-key-with-at-least-32-bytes"
TEST_SESSION_KEY = "test-session-key-with-at-least-32-bytes"


class TestClient(_BaseTestClient):
    """Exercise the clear-text transport path only as a loopback client."""

    def __init__(self, app, **kwargs):
        kwargs.setdefault("base_url", "http://127.0.0.1")
        kwargs.setdefault("client", ("127.0.0.1", 50_000))
        super().__init__(app, **kwargs)

    def websocket_connect(self, url, *args, **kwargs):
        if url.startswith("/"):
            url = f"ws://127.0.0.1{url}"
        return super().websocket_connect(url, *args, **kwargs)


def _start_demo(socket) -> None:
    hello = socket.receive_json()
    assert hello["type"] == "session.hello"
    assert hello["runtime"]["backends"] == {
        "vad": "energy",
        "asr": "demo",
        "llm": "demo",
        "tts": "browser",
        "avatar": "client_lipsync",
    }
    socket.send_json(
        {
            "type": "start",
            "persona_id": "demo",
            "access_token": TEST_ACCESS_TOKEN,
            "ai_disclosure_ack": True,
        }
    )
    ready_types = {socket.receive_json()["type"] for _ in range(3)}
    assert ready_types == {
        "session.negotiated",
        "session.state",
        "session.ready",
    }


def _signed_start(token: str) -> dict:
    return {
        "type": "start",
        "persona_id": "demo",
        "access_token": token,
        "ai_disclosure_ack": True,
        "protocol": {
            "version": 1,
            "capabilities": ["input.text", "output.text_stream"],
        },
    }


def _gateway_gauge(app, name: str) -> float:
    snapshot = app.state.observability.metrics.snapshot()
    return next(
        series["value"] for series in snapshot["gauges"] if series["name"] == name
    )


def _write_external_fictional_persona(root) -> None:
    persona_dir = root / "fictional"
    persona_dir.mkdir()
    skill_path = persona_dir / "SKILL.md"
    skill_path.write_text("# Reviewed fictional perspective", encoding="utf-8")
    now = datetime.now(timezone.utc)
    manifest = {
        "schema_version": 1,
        "manifest_revision": 1,
        "persona_id": "fictional",
        "consent_id": "fictional-test-consent",
        "subject_display_name": "Fictional Test",
        "profile_class": "fictional_original",
        "asset_rights_record_id": "test-original-art-record",
        "consent_granted": True,
        "consent_withdrawn": False,
        "consent_scope": [
            "interactive_conversation",
            "persona_profile",
            "voice_clone",
            "avatar_animation",
        ],
        "issued_at": (now - timedelta(minutes=1)).isoformat(),
        "valid_until": (now + timedelta(days=1)).isoformat(),
        "nuwa_skill": "SKILL.md",
        "reference_hashes": {
            "SKILL.md": hashlib.sha256(skill_path.read_bytes()).hexdigest()
        },
    }
    manifest_path = persona_dir / "consent.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    sign_manifest(manifest_path, TEST_SIGNING_KEY)


def test_health_does_not_expose_secrets(tmp_path):
    app = create_app(
        Settings(
            persona_root=tmp_path,
            deepseek_api_key="must-not-leak",
            consent_signing_key="also-private-but-at-least-32-bytes",
            consent_state_path=tmp_path / "consent-state.json",
        )
    )
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert set(payload) == {"ok", "service", "version"}
    assert "must-not-leak" not in response.text
    assert "also-private-but-at-least-32-bytes" not in response.text


def test_hello_reports_backend_ids_without_provider_details(tmp_path):
    settings = Settings(
        persona_root=tmp_path,
        access_token=TEST_ACCESS_TOKEN,
        llm_backend="openai_compatible",
        openai_compat_api_key="provider-secret-must-not-leak",
        openai_compat_base_url="https://shrimpai.cc/v1",
        openai_compat_model="step-3.7-flash",
    )
    app = create_app(settings)

    with TestClient(app).websocket_connect("/ws") as socket:
        hello = socket.receive_json()

    assert hello["runtime"]["backends"] == {
        "vad": "energy",
        "asr": "demo",
        "llm": "openai_compatible",
        "tts": "browser",
        "avatar": "client_lipsync",
    }
    serialized = json.dumps(hello)
    assert "provider-secret-must-not-leak" not in serialized
    assert "shrimpai.cc" not in serialized
    assert "step-3.7-flash" not in serialized


def test_docs_are_self_hosted_and_csp_compatible(tmp_path):
    app = create_app(Settings(persona_root=tmp_path))
    response = TestClient(app).get("/docs")

    assert response.status_code == 200
    assert "SELF-HOSTED API REFERENCE" in response.text
    assert "cdn.jsdelivr.net" not in response.text
    assert "<script" not in response.text
    csp = response.headers["content-security-policy"]
    assert csp.startswith("default-src 'self'")
    assert "connect-src 'self'" in csp
    assert " ws:" not in csp and " wss:" not in csp
    for directive in (
        "object-src 'none'",
        "worker-src 'self'",
        "manifest-src 'self'",
        "frame-src 'none'",
    ):
        assert directive in csp


def test_websocket_text_turn(tmp_path):
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))
    with TestClient(app).websocket_connect("/ws") as socket:
        _start_demo(socket)

        socket.send_json({"type": "text", "text": "端到端测试"})
        received = []
        for _ in range(100):
            event = socket.receive_json()
            received.append(event["type"])
            if event["type"] == "assistant.final":
                break
        assert "assistant.delta" in received
        assert "tts.browser" in received
        assert "avatar.segment" in received
        assert received[-1] == "assistant.final"


def test_external_persona_session_uses_its_immutable_grant(tmp_path):
    _write_external_fictional_persona(tmp_path)
    settings = Settings(
        persona_root=tmp_path,
        allowed_personas=("fictional",),
        session_signing_key=TEST_SESSION_KEY,
        consent_signing_key=TEST_SIGNING_KEY,
        consent_state_path=tmp_path / "consent-state.json",
    )
    app = create_app(settings)
    token = issue_session_token(
        TEST_SESSION_KEY,
        subject="persona-test-user",
        audience=settings.session_token_audience,
        persona_scope={"fictional"},
        capabilities={"input.text", "output.text_stream"},
        ttl_seconds=60,
    )
    with TestClient(app).websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "session.hello"
        socket.send_json(
            {
                "type": "start",
                "persona_id": "fictional",
                "access_token": token,
                "ai_disclosure_ack": True,
                "protocol": {
                    "version": 1,
                    "capabilities": ["input.text", "output.text_stream"],
                },
            }
        )
        ready_types = {socket.receive_json()["type"] for _ in range(3)}
        assert ready_types == {
            "session.negotiated",
            "session.state",
            "session.ready",
        }
        socket.send_json({"type": "text", "text": "grant wiring"})
        error_codes = []
        for _ in range(100):
            event = socket.receive_json()
            if event["type"] == "error":
                error_codes.append(event["code"])
            if event["type"] == "assistant.final":
                break
        assert "authorization_revoked" not in error_codes


def test_websocket_rejects_non_object_without_disconnect(tmp_path):
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))
    with TestClient(app).websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "session.hello"
        socket.send_json(["not", "a", "control", "object"])
        error = socket.receive_json()
        assert error["code"] == "invalid_control_json"
        socket.send_json(
            {
                "type": "start",
                "persona_id": "demo",
                "access_token": TEST_ACCESS_TOKEN,
                "ai_disclosure_ack": True,
            }
        )
        ready_types = {socket.receive_json()["type"] for _ in range(3)}
        assert ready_types == {
            "session.negotiated",
            "session.state",
            "session.ready",
        }


def test_disclosure_acknowledgement_requires_json_true(tmp_path):
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))
    with TestClient(app).websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "session.hello"
        socket.send_json(
            {
                "type": "start",
                "persona_id": "demo",
                "access_token": TEST_ACCESS_TOKEN,
                "ai_disclosure_ack": "true",
            }
        )
        error = socket.receive_json()
        assert error["code"] == "disclosure_not_acknowledged"


def test_local_origin_tracks_non_default_port(tmp_path):
    settings = Settings(
        persona_root=tmp_path,
        port=9877,
        access_token=TEST_ACCESS_TOKEN,
    )
    app = create_app(settings)
    with TestClient(app).websocket_connect(
        "/ws",
        headers={"origin": "http://127.0.0.1:9877"},
    ) as socket:
        assert socket.receive_json()["type"] == "session.hello"


def test_tokenless_websocket_rejects_non_loopback_peer(tmp_path):
    app = create_app(Settings(persona_root=tmp_path))
    with (
        pytest.raises(WebSocketDisconnect) as caught,
        _BaseTestClient(
            app,
            base_url="http://public.example",
            client=("203.0.113.10", 50_000),
        ).websocket_connect("ws://public.example/ws"),
    ):
        pass
    assert caught.value.code == 1008


def test_oversized_microphone_frame_is_rejected_and_closed(tmp_path):
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))
    with TestClient(app).websocket_connect("/ws") as socket:
        _start_demo(socket)
        socket.send_bytes(
            pack_packet(
                PacketKind.MIC_PCM16,
                0,
                0,
                b"\x00" * (MAX_MIC_PAYLOAD_BYTES + 2),
            )
        )
        error = socket.receive_json()
        assert error["code"] == "invalid_mic_frame"
        close_message = socket.receive()
        while close_message["type"] == "websocket.send":
            close_message = socket.receive()
        assert close_message["type"] == "websocket.close"
        assert close_message["code"] == 1008


def test_microphone_audio_is_limited_to_realtime_rate(tmp_path):
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))
    with TestClient(app).websocket_connect("/ws") as socket:
        _start_demo(socket)
        error = None
        for index in range(20):
            socket.send_bytes(
                pack_packet(
                    PacketKind.MIC_PCM16,
                    0,
                    index * 250,
                    b"\x00" * MAX_MIC_PAYLOAD_BYTES,
                )
            )
            event = socket.receive_json()
            if event["type"] == "error":
                error = event
                break
            assert event["type"] == "vad.level"
        assert error is not None
        assert error["code"] == "mic_rate_exceeded"


def test_signed_session_token_is_scoped_and_replay_protected(tmp_path):
    settings = Settings(
        persona_root=tmp_path,
        session_signing_key=TEST_SESSION_KEY,
    )
    app = create_app(settings)
    token = issue_session_token(
        TEST_SESSION_KEY,
        subject="test-user",
        audience=settings.session_token_audience,
        persona_scope={"demo"},
        capabilities={"input.text", "output.text_stream"},
        ttl_seconds=60,
    )
    start = {
        "type": "start",
        "persona_id": "demo",
        "access_token": token,
        "ai_disclosure_ack": True,
        "protocol": {
            "version": 1,
            "capabilities": ["input.text", "output.text_stream"],
        },
    }

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session.hello"
            socket.send_json(start)
            ready = {socket.receive_json()["type"] for _ in range(3)}
            assert ready == {
                "session.negotiated",
                "session.ready",
                "session.state",
            }

        with client.websocket_connect("/ws") as replayed:
            assert replayed.receive_json()["type"] == "session.hello"
            replayed.send_json(start)
            error = replayed.receive_json()
            assert error["code"] == "authentication_failed"
            assert TEST_SESSION_KEY not in json.dumps(error)


def test_signed_session_mode_does_not_accept_legacy_static_token(tmp_path):
    app = create_app(
        Settings(
            persona_root=tmp_path,
            access_token=TEST_ACCESS_TOKEN,
            session_signing_key=TEST_SESSION_KEY,
        )
    )
    with TestClient(app).websocket_connect("/ws") as socket:
        assert socket.receive_json()["type"] == "session.hello"
        socket.send_json(
            {
                "type": "start",
                "persona_id": "demo",
                "access_token": TEST_ACCESS_TOKEN,
                "ai_disclosure_ack": True,
            }
        )
        assert socket.receive_json()["code"] == "authentication_failed"


def test_non_demo_persona_cannot_be_exposed_by_one_global_static_token(tmp_path):
    with pytest.raises(ValueError, match="SESSION_SIGNING_KEY"):
        create_app(
            Settings(
                persona_root=tmp_path,
                allowed_personas=("demo", "real-person"),
                access_token=TEST_ACCESS_TOKEN,
            )
        )


def test_global_session_admission_releases_slot_after_disconnect(tmp_path):
    app = create_app(
        Settings(
            persona_root=tmp_path,
            access_token=TEST_ACCESS_TOKEN,
            max_active_sessions=1,
            max_connections_per_ip=4,
        )
    )
    with (
        TestClient(app) as client,
        client.websocket_connect("/ws") as anonymous_idle,
    ):
        assert anonymous_idle.receive_json()["type"] == "session.hello"
        with client.websocket_connect("/ws") as active:
            _start_demo(active)
            with client.websocket_connect("/ws") as overloaded:
                assert overloaded.receive_json()["type"] == "session.hello"
                overloaded.send_json(
                    {
                        "type": "start",
                        "persona_id": "demo",
                        "access_token": TEST_ACCESS_TOKEN,
                        "ai_disclosure_ack": True,
                    }
                )
                error = overloaded.receive_json()
                assert error["code"] == "server_overloaded"
                assert error["fatal"] is True
                assert error["retryable"] is True
                closed = overloaded.receive()
                assert closed["type"] == "websocket.close"
                assert closed["code"] == 1013

        with client.websocket_connect("/ws") as after_disconnect:
            _start_demo(after_disconnect)

    assert _gateway_gauge(app, "gateway.sessions.pending") == 0
    assert _gateway_gauge(app, "gateway.sessions.active") == 0


def test_replayed_session_token_never_reaches_runtime_acquire(tmp_path):
    settings = Settings(
        persona_root=tmp_path,
        session_signing_key=TEST_SESSION_KEY,
        max_active_sessions=2,
        max_connections_per_ip=4,
    )
    app = create_app(settings)
    token = issue_session_token(
        TEST_SESSION_KEY,
        subject="concurrent-replay-test",
        audience=settings.session_token_audience,
        persona_scope={"demo"},
        capabilities={"input.text", "output.text_stream"},
        ttl_seconds=60,
    )
    start = _signed_start(token)
    acquire_entered = threading.Event()
    release_acquire = threading.Event()
    acquire_calls = 0

    with TestClient(app) as client:
        runtime_factory = app.state.runtime_factory
        original_acquire = runtime_factory.acquire

        async def blocked_acquire(timeout_seconds):
            nonlocal acquire_calls
            acquire_calls += 1
            acquire_entered.set()
            await asyncio.to_thread(release_acquire.wait)
            return await original_acquire(timeout_seconds)

        runtime_factory.acquire = blocked_acquire
        try:
            with client.websocket_connect("/ws") as first:
                assert first.receive_json()["type"] == "session.hello"
                first.send_json(start)
                assert acquire_entered.wait(timeout=2)

                with client.websocket_connect("/ws") as replay:
                    assert replay.receive_json()["type"] == "session.hello"
                    replay.send_json(start)
                    error = replay.receive_json()
                    assert error["code"] == "authentication_failed"
                    assert acquire_calls == 1

                release_acquire.set()
                ready = {first.receive_json()["type"] for _ in range(3)}
                assert ready == {
                    "session.negotiated",
                    "session.ready",
                    "session.state",
                }
        finally:
            release_acquire.set()

    assert acquire_calls == 1
    assert _gateway_gauge(app, "gateway.sessions.pending") == 0
    assert _gateway_gauge(app, "gateway.sessions.active") == 0


def test_pending_runtime_acquire_counts_toward_session_capacity(tmp_path):
    settings = Settings(
        persona_root=tmp_path,
        session_signing_key=TEST_SESSION_KEY,
        max_active_sessions=1,
        max_connections_per_ip=4,
    )
    app = create_app(settings)

    def new_token(subject: str) -> str:
        return issue_session_token(
            TEST_SESSION_KEY,
            subject=subject,
            audience=settings.session_token_audience,
            persona_scope={"demo"},
            capabilities={"input.text", "output.text_stream"},
            ttl_seconds=60,
        )

    acquire_entered = threading.Event()
    release_acquire = threading.Event()
    acquire_calls = 0

    with TestClient(app) as client:
        runtime_factory = app.state.runtime_factory
        original_acquire = runtime_factory.acquire

        async def block_first_acquire(timeout_seconds):
            nonlocal acquire_calls
            acquire_calls += 1
            if acquire_calls == 1:
                acquire_entered.set()
                await asyncio.to_thread(release_acquire.wait)
            return await original_acquire(timeout_seconds)

        runtime_factory.acquire = block_first_acquire
        try:
            with client.websocket_connect("/ws") as first:
                assert first.receive_json()["type"] == "session.hello"
                first.send_json(_signed_start(new_token("pending-first")))
                assert acquire_entered.wait(timeout=2)
                assert _gateway_gauge(app, "gateway.sessions.pending") == 1

                with client.websocket_connect("/ws") as second:
                    assert second.receive_json()["type"] == "session.hello"
                    second.send_json(_signed_start(new_token("pending-second")))
                    error = second.receive_json()
                    assert error["code"] == "server_overloaded"
                    assert error["fatal"] is True
                    assert "new session token" in error["message"]
                    assert acquire_calls == 1
                    closed = second.receive()
                    assert closed["type"] == "websocket.close"
                    assert closed["code"] == 1013

                release_acquire.set()
                ready = {first.receive_json()["type"] for _ in range(3)}
                assert ready == {
                    "session.negotiated",
                    "session.ready",
                    "session.state",
                }

            with client.websocket_connect("/ws") as after_release:
                assert after_release.receive_json()["type"] == "session.hello"
                after_release.send_json(_signed_start(new_token("pending-third")))
                ready = {after_release.receive_json()["type"] for _ in range(3)}
                assert ready == {
                    "session.negotiated",
                    "session.ready",
                    "session.state",
                }
        finally:
            release_acquire.set()

    assert acquire_calls == 2
    assert _gateway_gauge(app, "gateway.sessions.pending") == 0
    assert _gateway_gauge(app, "gateway.sessions.active") == 0


def test_runtime_failure_releases_pending_slot_but_consumes_token(tmp_path):
    settings = Settings(
        persona_root=tmp_path,
        session_signing_key=TEST_SESSION_KEY,
        max_active_sessions=1,
    )
    app = create_app(settings)

    def new_token(subject: str) -> str:
        return issue_session_token(
            TEST_SESSION_KEY,
            subject=subject,
            audience=settings.session_token_audience,
            persona_scope={"demo"},
            capabilities={"input.text", "output.text_stream"},
            ttl_seconds=60,
        )

    failed_token = new_token("runtime-failure")
    acquire_calls = 0
    with TestClient(app) as client:
        runtime_factory = app.state.runtime_factory
        original_acquire = runtime_factory.acquire

        async def fail_first_acquire(timeout_seconds):
            nonlocal acquire_calls
            acquire_calls += 1
            if acquire_calls == 1:
                raise RuntimeUnavailable("rt_expected_test_failure")
            return await original_acquire(timeout_seconds)

        runtime_factory.acquire = fail_first_acquire
        with client.websocket_connect("/ws") as socket:
            assert socket.receive_json()["type"] == "session.hello"
            socket.send_json(_signed_start(failed_token))
            unavailable = socket.receive_json()
            assert unavailable["code"] == "runtime_unavailable"
            assert "new session token" in unavailable["message"]
            assert _gateway_gauge(app, "gateway.sessions.pending") == 0

            socket.send_json(_signed_start(failed_token))
            assert socket.receive_json()["code"] == "authentication_failed"
            assert acquire_calls == 1

            socket.send_json(_signed_start(new_token("runtime-retry")))
            ready = {socket.receive_json()["type"] for _ in range(3)}
            assert ready == {
                "session.negotiated",
                "session.ready",
                "session.state",
            }

    assert acquire_calls == 2
    assert _gateway_gauge(app, "gateway.sessions.pending") == 0
    assert _gateway_gauge(app, "gateway.sessions.active") == 0


def test_active_session_stops_when_session_token_expires(tmp_path):
    settings = Settings(
        persona_root=tmp_path,
        session_signing_key=TEST_SESSION_KEY,
        session_token_clock_skew_seconds=0,
        max_session_seconds=60,
    )
    token = issue_session_token(
        TEST_SESSION_KEY,
        subject="short-session",
        audience=settings.session_token_audience,
        persona_scope={"demo"},
        capabilities={"input.text", "output.text_stream"},
        ttl_seconds=2,
    )

    with (
        TestClient(create_app(settings)) as client,
        client.websocket_connect("/ws") as socket,
    ):
        assert socket.receive_json()["type"] == "session.hello"
        socket.send_json(_signed_start(token))
        ready = {socket.receive_json()["type"] for _ in range(3)}
        assert ready == {
            "session.negotiated",
            "session.ready",
            "session.state",
        }
        expired = socket.receive_json()
        assert expired["type"] == "error"
        assert expired["code"] == "session_expired"
        assert expired["fatal"] is True
        assert expired["message"] == "session authorization expired"
        closed = socket.receive()
        for _ in range(4):
            if closed["type"] == "websocket.close":
                break
            closed = socket.receive()
        assert closed["type"] == "websocket.close"
        assert closed["code"] == 1000
