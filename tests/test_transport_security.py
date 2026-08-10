from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from echoweave import cli
from echoweave.app import create_app
from echoweave.config import Settings

TEST_ACCESS_TOKEN = "transport-test-token-with-at-least-32-bytes"


def test_cleartext_loopback_http_and_websocket_remain_available(tmp_path):
    app = create_app(Settings(persona_root=tmp_path))
    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50_000),
    ) as client:
        assert client.get("/api/health/live").status_code == 200
        with client.websocket_connect("ws://127.0.0.1/ws") as socket:
            assert socket.receive_json()["type"] == "session.hello"


def test_nonloopback_cleartext_is_rejected_even_with_forwarded_header(tmp_path):
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))
    with TestClient(
        app,
        base_url="http://public.example",
        client=("203.0.113.10", 50_000),
    ) as client:
        response = client.get(
            "/api/health/live",
            headers={
                "Forwarded": "for=203.0.113.10;proto=https",
                "X-Forwarded-Proto": "https",
            },
        )
        assert response.status_code == 426
        assert response.headers["cache-control"] == "no-store"

        with (
            pytest.raises(WebSocketDisconnect) as caught,
            client.websocket_connect(
                "ws://public.example/ws",
                headers={
                    "Forwarded": "for=203.0.113.10;proto=https",
                    "X-Forwarded-Proto": "https",
                },
            ),
        ):
            pass
        assert caught.value.code == 1008


def test_native_nonloopback_https_and_wss_are_allowed(tmp_path):
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))
    with TestClient(
        app,
        base_url="https://public.example",
        client=("203.0.113.10", 50_000),
    ) as client:
        response = client.get("/api/health/live")
        assert response.status_code == 200
        assert response.headers["strict-transport-security"].startswith(
            "max-age=31536000"
        )
        with client.websocket_connect("wss://public.example/ws") as socket:
            assert socket.receive_json()["type"] == "session.hello"


def test_only_explicitly_trusted_uvicorn_proxy_can_rewrite_scheme(tmp_path):
    app = create_app(Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN))
    proxied = ProxyHeadersMiddleware(app, trusted_hosts=["10.0.0.0/8"])

    with TestClient(
        proxied,
        base_url="http://public.example",
        client=("10.0.0.10", 50_000),
    ) as trusted:
        response = trusted.get(
            "/api/health/live",
            headers={"X-Forwarded-Proto": "https"},
        )
        assert response.status_code == 200
        with trusted.websocket_connect(
            "ws://public.example/ws",
            headers={"X-Forwarded-Proto": "https"},
        ) as socket:
            assert socket.receive_json()["type"] == "session.hello"

    with TestClient(
        proxied,
        base_url="http://public.example",
        client=("203.0.113.10", 50_000),
    ) as untrusted:
        response = untrusted.get(
            "/api/health/live",
            headers={"X-Forwarded-Proto": "https"},
        )
        assert response.status_code == 426


def test_explicit_insecure_override_is_limited_to_private_addresses(tmp_path):
    default_app = create_app(
        Settings(persona_root=tmp_path, access_token=TEST_ACCESS_TOKEN)
    )
    with TestClient(
        default_app,
        base_url="http://192.168.50.10",
        client=("192.168.50.20", 50_000),
    ) as rejected_private_client:
        assert rejected_private_client.get("/api/health/live").status_code == 426

    app = create_app(
        Settings(
            persona_root=tmp_path,
            access_token=TEST_ACCESS_TOKEN,
            allow_insecure_private_transport=True,
        )
    )
    with TestClient(
        app,
        base_url="http://192.168.50.10",
        client=("192.168.50.20", 50_000),
    ) as private_client:
        assert private_client.get("/api/health/live").status_code == 200
        with private_client.websocket_connect("ws://192.168.50.10/ws") as socket:
            assert socket.receive_json()["type"] == "session.hello"

    with TestClient(
        app,
        base_url="http://public.example",
        client=("203.0.113.10", 50_000),
    ) as public_client:
        assert public_client.get("/api/health/live").status_code == 426


def test_transport_environment_boolean_is_strict(monkeypatch):
    monkeypatch.setenv("ECHOWEAVE_ALLOW_INSECURE_PRIVATE_TRANSPORT", "1")

    with pytest.raises(ValueError, match="exactly 'true' or 'false'"):
        Settings.from_env()

    monkeypatch.setenv("ECHOWEAVE_ALLOW_INSECURE_PRIVATE_TRANSPORT", "true")
    assert Settings.from_env().allow_insecure_private_transport is True


def test_openai_compatible_environment_is_explicit_and_https(monkeypatch):
    monkeypatch.setenv("ECHOWEAVE_LLM_BACKEND", "openai_compatible")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "server-secret")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://shrimpai.cc/v1/")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "step-3.7-flash")
    monkeypatch.setenv("OPENAI_COMPAT_MAX_TOKENS", "512")

    settings = Settings.from_env()
    settings.validate()

    assert settings.openai_compat_base_url == "https://shrimpai.cc/v1"
    assert settings.openai_compat_model == "step-3.7-flash"
    assert settings.openai_compat_max_tokens == 512


@pytest.mark.parametrize(
    "base_url",
    [
        "http://shrimpai.cc/v1",
        "https://user:pass@shrimpai.cc/v1",
        "https://shrimpai.cc/v1?key=secret",
        "https://shrimpai.cc/v1#fragment",
        "not-a-url",
    ],
)
def test_openai_compatible_backend_rejects_unsafe_base_urls(base_url):
    settings = Settings(
        llm_backend="openai_compatible",
        openai_compat_api_key="server-secret",
        openai_compat_base_url=base_url,
        openai_compat_model="fast-model",
    )

    with pytest.raises(ValueError, match="OPENAI_COMPAT_BASE_URL"):
        settings.validate()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.deepseek.com",
        "https://user:pass@api.deepseek.com",
        "https://api.deepseek.com?key=secret",
        "https://api.deepseek.com#fragment",
        "not-a-url",
    ],
)
def test_deepseek_backend_rejects_unsafe_base_urls(base_url):
    settings = Settings(
        llm_backend="deepseek",
        deepseek_api_key="server-secret",
        deepseek_base_url=base_url,
    )

    with pytest.raises(ValueError, match="DEEPSEEK_BASE_URL"):
        settings.validate()


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("openai_compat_api_key", "OPENAI_COMPAT_API_KEY"),
        ("openai_compat_base_url", "OPENAI_COMPAT_BASE_URL"),
        ("openai_compat_model", "OPENAI_COMPAT_MODEL"),
    ],
)
def test_openai_compatible_backend_requires_all_provider_settings(field, message):
    values = {
        "openai_compat_api_key": "server-secret",
        "openai_compat_base_url": "https://gateway.test/v1",
        "openai_compat_model": "fast-model",
    }
    values[field] = ""

    with pytest.raises(ValueError, match=message):
        Settings(llm_backend="openai_compatible", **values).validate()


@pytest.mark.parametrize(
    "value",
    ["*", "proxy.internal", "0.0.0.0", "0.0.0.0/0", "10.1.2.3/8"],
)
def test_trusted_proxy_configuration_accepts_only_strict_ips_and_cidrs(value):
    settings = Settings(trusted_proxy_ips=(value,))

    with pytest.raises(ValueError, match="trusted proxy"):
        settings.validate()


def test_trusted_proxy_configuration_is_canonicalized():
    settings = Settings(trusted_proxy_ips=("127.0.0.1", "10.0.0.0/8", "2001:db8::1"))
    settings.validate()

    assert settings.normalized_trusted_proxy_ips == (
        "127.0.0.1",
        "10.0.0.0/8",
        "2001:db8::1",
    )


def test_nonloopback_cli_bind_requires_an_explicit_secure_mode():
    settings = Settings(access_token=TEST_ACCESS_TOKEN)

    with pytest.raises(RuntimeError, match="non-loopback bind requires TLS"):
        settings.validate_bind_host("0.0.0.0")


def test_nonloopback_bind_still_requires_authentication_with_tls(tmp_path):
    certfile = tmp_path / "gateway.crt"
    keyfile = tmp_path / "gateway.key"
    certfile.write_text("test certificate", encoding="utf-8")
    keyfile.write_text("test key", encoding="utf-8")

    with pytest.raises(RuntimeError, match="session authentication is required"):
        Settings().validate_bind_host(
            "0.0.0.0",
            tls_certfile=certfile,
            tls_keyfile=keyfile,
        )


def test_tls_certificate_and_key_must_be_configured_together(tmp_path):
    certfile = tmp_path / "gateway.crt"
    certfile.write_text("test certificate", encoding="utf-8")

    with pytest.raises(ValueError, match="configured together"):
        Settings(tls_certfile=certfile).validate()


def test_cli_passes_tls_and_explicit_proxy_configuration(
    tmp_path,
    monkeypatch,
):
    certfile = tmp_path / "gateway.crt"
    keyfile = tmp_path / "gateway.key"
    certfile.write_text("test certificate", encoding="utf-8")
    keyfile.write_text("test key", encoding="utf-8")
    settings = Settings(
        access_token=TEST_ACCESS_TOKEN,
        trusted_proxy_ips=("127.0.0.1", "10.0.0.0/8"),
    )
    captured = {}

    monkeypatch.setattr(cli.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda app, **kwargs: captured.update({"app": app, **kwargs}),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "echoweave",
            "serve",
            "--host",
            "0.0.0.0",
            "--ssl-certfile",
            str(certfile),
            "--ssl-keyfile",
            str(keyfile),
        ],
    )
    for name in (
        "ECHOWEAVE_HOST",
        "ECHOWEAVE_PORT",
        "ECHOWEAVE_TLS_CERTFILE",
        "ECHOWEAVE_TLS_KEYFILE",
    ):
        monkeypatch.setenv(name, "test-restore-value")

    cli.main()

    assert captured["app"] == "echoweave.app:app"
    assert captured["host"] == "0.0.0.0"
    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == ["127.0.0.1", "10.0.0.0/8"]
    assert Path(captured["ssl_certfile"]) == certfile
    assert Path(captured["ssl_keyfile"]) == keyfile


def test_cli_disables_proxy_header_processing_without_explicit_trust(monkeypatch):
    settings = Settings()
    captured = {}
    monkeypatch.setattr(cli.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda app, **kwargs: captured.update({"app": app, **kwargs}),
    )
    monkeypatch.setattr(sys, "argv", ["echoweave", "serve"])
    monkeypatch.setenv("ECHOWEAVE_HOST", "test-restore-value")
    monkeypatch.setenv("ECHOWEAVE_PORT", "test-restore-value")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")

    cli.main()

    assert captured["proxy_headers"] is False
    assert captured["forwarded_allow_ips"] == []
    assert captured["ssl_certfile"] is None
    assert captured["ssl_keyfile"] is None
