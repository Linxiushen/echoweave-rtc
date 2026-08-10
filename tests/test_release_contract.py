from __future__ import annotations

import re
from pathlib import Path

import tomllib

from echoweave import __version__

ROOT = Path(__file__).resolve().parents[1]
WEB_FILES = {"api.html", "app.js", "index.html", "mic-worklet.js", "styles.css"}


def test_release_versions_and_static_assets_stay_in_sync():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    assert version == __version__

    web_root = ROOT / "src" / "echoweave" / "web"
    assert WEB_FILES <= {path.name for path in web_root.iterdir() if path.is_file()}
    assert all((web_root / name).stat().st_size > 0 for name in WEB_FILES)

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f'org.opencontainers.image.version="{version}"' in dockerfile
    assert re.search(r"python:3\.12-slim@sha256:[0-9a-f]{64}", dockerfile)
    assert "USER echoweave" in dockerfile
    assert "HEALTHCHECK" in dockerfile

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f"--expected-version {version}" in workflow
    for test_name in (
        "test_app_lifecycle.mjs",
        "test_frontend_contract.mjs",
        "test_mic_worklet.mjs",
    ):
        assert test_name in workflow
    assert not re.search(r"uses:\s+[^\s]+@v\d+\s*$", workflow, re.MULTILINE)

    silero_license = ROOT / "LICENSES" / "Silero-MIT.txt"
    assert "Copyright (c) 2020-present Silero Team" in silero_license.read_text(
        encoding="utf-8"
    )
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "snakers4/silero-vad v5.1.2" in notice
    vad_source = (ROOT / "src" / "echoweave" / "adapters" / "vad.py").read_text(
        encoding="utf-8"
    )
    assert "6478567951ae5c9979ad7b234185b5515f4be7a1" in vad_source
    covenant_notice = (
        ROOT / "LICENSES" / "Contributor-Covenant-CC-BY-4.0.txt"
    ).read_text(encoding="utf-8")
    assert "Creative Commons Attribution 4.0" in covenant_notice
    assert "Contributor Covenant contributors" in notice


def test_docker_context_is_an_allowlist_without_local_secrets():
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert dockerignore[0] == "*"
    assert "!constraints/**" in dockerignore
    assert "!src/**" in dockerignore
    assert not any(line in {"!.env", "!.env.*"} for line in dockerignore)


def test_example_environment_and_compose_keep_identity_boundaries_explicit():
    env_lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    env_values = {
        key: value
        for line in env_lines
        if line and not line.startswith("#") and "=" in line
        for key, value in [line.split("=", 1)]
    }
    required = {
        "ECHOWEAVE_SESSION_SIGNING_KEY",
        "ECHOWEAVE_SESSION_TOKEN_AUDIENCE",
        "ECHOWEAVE_TRUSTED_PROXY_IPS",
        "ECHOWEAVE_ALLOW_INSECURE_PRIVATE_TRANSPORT",
        "ECHOWEAVE_TLS_CERTFILE",
        "ECHOWEAVE_TLS_KEYFILE",
        "ECHOWEAVE_MAX_ACTIVE_SESSIONS",
        "ECHOWEAVE_WORKER_ASSERTION_TTL_SECONDS",
        "OPENAI_COMPAT_API_KEY",
        "OPENAI_COMPAT_BASE_URL",
        "OPENAI_COMPAT_MODEL",
        "OPENAI_COMPAT_MAX_TOKENS",
        "VOXCPM_WORKER_TOKEN",
        "VOXCPM_WORKER_AUDIENCE",
        "VOXCPM_MAX_INFLIGHT_REQUESTS",
        "VOXCPM_REQUEST_BODY_TIMEOUT_SECONDS",
        "VOXCPM_PRODUCER_JOIN_TIMEOUT_SECONDS",
        "SOULX_WORKER_TOKEN",
        "SOULX_WORKER_AUDIENCE",
        "SOULX_MAX_INFLIGHT_REQUESTS",
        "SOULX_REQUEST_BODY_TIMEOUT_SECONDS",
    }
    assert required <= env_values.keys()
    for name in (
        "DEEPSEEK_API_KEY",
        "OPENAI_COMPAT_API_KEY",
        "ECHOWEAVE_ACCESS_TOKEN",
        "ECHOWEAVE_CONSENT_SIGNING_KEY",
        "ECHOWEAVE_SESSION_SIGNING_KEY",
        "MODEL_WORKER_TOKEN",
        "VOXCPM_WORKER_TOKEN",
        "SOULX_WORKER_TOKEN",
    ):
        assert env_values[name] == ""

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"127.0.0.1:8765:8765"' in compose
    assert '"8765:8765"' not in compose
    assert 'ECHOWEAVE_ALLOW_INSECURE_PRIVATE_TRANSPORT: "true"' in compose
