# Deployment

## 1. Gateway / safe demo

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\echoweave serve
```

Open `http://127.0.0.1:8765`. This mode uses dependency-free endpointing, a
deterministic demo responder, browser speech synthesis and a synthetic face.
If a demo access token or a scoped session token is required, open
`http://host:8765/#token=<short-lived-value>`; the UI copies the fragment into
memory and immediately removes it from the address bar. Select only a persona
ID listed in `ECHOWEAVE_ALLOWED_PERSONAS`.

The browser captures microphone audio with AudioWorklet when available and sends
16 kHz PCM16 in 20 ms frames. ScriptProcessor is only a compatibility fallback.
The UI keeps the AI/synthetic-media disclosure visible in both paths.

## 2. Silero v5

```powershell
.\.venv\Scripts\python -m pip install -e ".[silero-v5]"
$env:ECHOWEAVE_VAD_BACKEND = "silero_v5"
```

The adapter downloads the 2.3 MB ONNX file from the official upstream
`v5.1.2` Git tag, verifies its pinned SHA-256, and caches it under
`runtime/models/`. Set `SILERO_V5_MODEL_PATH` for an offline deployment.

## 3. Qwen3-ASR worker

First create an approved local snapshot at the revision recorded in
`docs/MODELS.md`; do not let a production worker resolve a floating branch:

```bash
huggingface-cli download Qwen/Qwen3-ASR-1.7B \
  --revision 7278e1e70fe206f11671096ffdd38061171dd6e5 \
  --local-dir /models/Qwen3-ASR-1.7B
```

Then use the official isolated environment and local model path:

```bash
pip install "qwen-asr[vllm]==0.0.6"
HF_HUB_OFFLINE=1 qwen-asr-serve /models/Qwen3-ASR-1.7B \
  --gpu-memory-utilization 0.8 --host 0.0.0.0 --port 8001
```

Gateway:

```text
ECHOWEAVE_ASR_BACKEND=qwen_http
QWEN_ASR_BASE_URL=http://asr-worker:8001/v1
```

## 4. DeepSeek V4 Flash

After rotating any previously exposed key:

```text
ECHOWEAVE_LLM_BACKEND=deepseek
DEEPSEEK_API_KEY=<server secret>
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_THINKING=disabled
```

Disabling thinking is recommended for ordinary realtime turns because it
reduces first-token latency.
Because this sends the reviewed persona profile and conversation to a hosted
processor, non-demo personas must include both `third_party_model_processing`
and the processor binding derived from the configured hostname in the signed
consent scope. With the default URL, that binding is
`third_party_processor:api.deepseek.com`.

### OpenAI-compatible hosted gateway

Use the generic backend for a provider that documents streaming OpenAI
chat-completions. Unlike the DeepSeek adapter, it does not send vendor-specific
`thinking` fields:

```text
ECHOWEAVE_LLM_BACKEND=openai_compatible
OPENAI_COMPAT_API_KEY=<server secret>
OPENAI_COMPAT_BASE_URL=https://provider.example/v1
OPENAI_COMPAT_MODEL=<exact provider model ID>
OPENAI_COMPAT_MAX_TOKENS=800
```

The base URL must use HTTPS and cannot contain credentials, a query string or a
fragment. Confirm the exact model ID through the provider before enabling the
backend. OpenAI-compatible chat does not imply compatibility with
`/audio/transcriptions`, `/audio/speech`, image generation, video generation or
EchoWeave's VoxCPM2 and SoulX worker protocols. Qualify each media endpoint and
adapter independently. Hosted processing requires both
`third_party_model_processing` and a processor binding derived from the exact
configured hostname. For the example above that binding is
`third_party_processor:provider.example`. Changing the hostname invalidates the
old authorization until a new manifest revision is reviewed and signed.

## 5. VoxCPM2 worker

For local Python integration, install `.[voxcpm]` and select `voxcpm_local`.
Download and hash the audited snapshot before starting the worker:

```bash
huggingface-cli download openbmb/VoxCPM2 \
  --revision bffb3df5a29440629464e5e839f4d214c8714c3d \
  --local-dir /models/VoxCPM2
```

Although the upstream vLLM-Omni endpoint can stream 48 kHz PCM and accept
`ref_audio`, it does not validate EchoWeave's consent assertion. Do not connect
it directly when voice cloning is enabled. Run the included authenticated
worker in an isolated VoxCPM environment:

```bash
pip install -e ".[voxcpm]"
VOXCPM_WORKER_TOKEN='<voxcpm-only-long-random-signing-key>' \
VOXCPM_WORKER_AUDIENCE='echoweave-voxcpm-worker' \
uvicorn services.voxcpm_api:app --host 0.0.0.0 --port 8002
```

Gateway:

```text
ECHOWEAVE_TTS_BACKEND=voxcpm_http
VOXCPM_BASE_URL=http://tts-worker:8002/v1
VOXCPM_WORKER_TOKEN=<same-voxcpm-only-signing-key>
VOXCPM_WORKER_AUDIENCE=echoweave-voxcpm-worker
```

The gateway sends a consent-bound reference clip to the private worker as a
base64 data URI and never exposes it to the browser. Keep this worker on a
private network and set the same `VOXCPM_WORKER_TOKEN` and audience on gateway
and worker. The value is a signing key, not a bearer credential: the gateway
uses it to mint a short-lived, one-time consent assertion bound to the persona,
manifest revision, scope and reference-audio hash. For real-person voice
cloning, use this authenticated bridge; a stock vLLM endpoint does not enforce
EchoWeave consent assertions by itself.

The worker admits at most `VOXCPM_MAX_INFLIGHT_REQUESTS` requests, bounds body
read time with `VOXCPM_REQUEST_BODY_TIMEOUT_SECONDS`, and waits at most
`VOXCPM_PRODUCER_JOIN_TIMEOUT_SECONDS` for a cancelled native iterator. If that
iterator does not stop, the worker quarantines itself, readiness fails and the
supervisor must replace the process; it never accepts new cloning work while a
stuck GPU thread may still exist.

## 6. SoulX worker

Follow the official Linux setup:

```bash
git clone https://github.com/Soul-AILab/SoulX-FlashHead.git
conda create -n flashhead python=3.10 -y
conda activate flashhead
pip install torch==2.7.1 torchvision==0.22.1 \
  --index-url https://download.pytorch.org/whl/cu128
pip install -r SoulX-FlashHead/requirements.txt
pip install flash_attn==2.8.0.post2 --no-build-isolation
huggingface-cli download Soul-AILab/SoulX-FlashHead-1_3B \
  --revision 59119b6c681230c3eeee157e224ae1941746711e \
  --local-dir /models/SoulX-FlashHead-1_3B
huggingface-cli download facebook/wav2vec2-base-960h \
  --revision 22aad52d435eb6dbaf354bdad9b0da84ce7d6156 \
  --local-dir /models/wav2vec2-base-960h
```

Start the provided bridge from an environment that also has FastAPI/Uvicorn:

```bash
SOULX_REPO_DIR=/opt/SoulX-FlashHead \
SOULX_CKPT_DIR=/models/SoulX-FlashHead-1_3B \
SOULX_WAV2VEC_DIR=/models/wav2vec2-base-960h \
SOULX_WORKER_TOKEN='<different-soulx-only-long-random-signing-key>' \
SOULX_WORKER_AUDIENCE='echoweave-soulx-worker' \
uvicorn services.soulx_api:app --host 0.0.0.0 --port 8003
```

Gateway:

```text
ECHOWEAVE_AVATAR_BACKEND=soulx_http
SOULX_BASE_URL=http://avatar-worker:8003
SOULX_WORKER_TOKEN=<same-soulx-only-signing-key>
SOULX_WORKER_AUDIENCE=echoweave-soulx-worker
```

The bridge validates uploads and official-model output paths, serializes GPU
inference, and burns `AI DIGITAL TWIN` into every MP4. It uses the official
Gradio streaming generator, whose segments are roughly three seconds; for
sub-second production video latency, replace only this worker boundary with a
raw-frame SoulX service while keeping the gateway protocol and authorization
checks.

## 7. Internet-facing gateway

The gateway fails closed on transport before authentication: non-loopback
clear-text HTTP receives `426 Upgrade Required`, and a clear-text WebSocket is
closed before acceptance with code `1008`. Loopback HTTP/WS remains available
for local development. Native HTTPS/WSS and normalized ASGI scopes from an
explicitly trusted TLS proxy are accepted; HTTPS responses include HSTS.

The `echoweave` CLI refuses a non-loopback bind unless authentication is set
and one of the following secure transport modes is explicit:

1. Native TLS: configure both `ECHOWEAVE_TLS_CERTFILE` and
   `ECHOWEAVE_TLS_KEYFILE`, or pass the matching `--ssl-certfile` and
   `--ssl-keyfile` options.
2. TLS reverse proxy: set `ECHOWEAVE_TRUSTED_PROXY_IPS` to the exact direct
   proxy IPs or strict CIDRs. Proxy-header processing is disabled when this
   list is empty. The application never reads `Forwarded` or
   `X-Forwarded-*` itself, so an untrusted client cannot promote a request by
   forging those headers.
3. Isolated development network only: set
   `ECHOWEAVE_ALLOW_INSECURE_PRIVATE_TRANSPORT=true`. This exception requires
   the client, server socket and requested Host to all be loopback, RFC1918 or
   IPv6 ULA; public destinations remain blocked. Do not use it as internet
   transport security.

Any allowlisted non-demo persona requires the session signing key and accepts
only short-lived subject/persona/capability-scoped tokens; a shared access token
cannot start it. Also configure exact `ECHOWEAVE_ALLOWED_ORIGINS` and a private
network policy for all model workers.

Example native TLS bind:

```text
ECHOWEAVE_HOST=0.0.0.0
ECHOWEAVE_TLS_CERTFILE=/run/secrets/gateway.crt
ECHOWEAVE_TLS_KEYFILE=/run/secrets/gateway.key
```

Example reverse-proxy boundary, where `10.20.0.5` is the only direct proxy:

```text
ECHOWEAVE_HOST=0.0.0.0
ECHOWEAVE_TRUSTED_PROXY_IPS=10.20.0.5
ECHOWEAVE_ALLOWED_ORIGINS=https://agent.example.com
```

Issue a session token only in a trusted control plane. The signing key stays on
the server; only the returned short-lived token is copied into the UI (or a
`#token=` fragment that the UI immediately removes). For example:

```bash
SESSION_TOKEN="$(python - <<'PY'
import os
from echoweave.auth import issue_session_token

print(issue_session_token(
    os.environ["ECHOWEAVE_SESSION_SIGNING_KEY"],
    subject="user-123",
    audience=os.environ.get("ECHOWEAVE_SESSION_TOKEN_AUDIENCE", "echoweave-rtc"),
    persona_scope={"authorized-alex"},
    capabilities={
        "control.barge_in", "control.cancel", "control.ping",
        "input.audio_pcm16", "input.text", "output.audio_pcm16",
        "output.avatar_events", "output.browser_tts",
        "output.text_stream", "output.video_fragments",
    },
    ttl_seconds=120,
))
PY
)"
```

Do not mint broad persona sets for convenience. A token is consumed once after
authorization and before capacity reservation or runtime acquisition;
reconnection requires a newly issued token. Its `exp` also caps the active
session deadline; expiry emits `session_expired`, stops generation and closes
the socket even when `ECHOWEAVE_MAX_SESSION_SECONDS` is longer.

Do not place tokens in query parameters, reverse-proxy configuration committed
to source control, container images or browser bundles. Inject
`ECHOWEAVE_ACCESS_TOKEN`, `ECHOWEAVE_CONSENT_SIGNING_KEY`,
`ECHOWEAVE_SESSION_SIGNING_KEY`, `VOXCPM_WORKER_TOKEN`,
`SOULX_WORKER_TOKEN`, `DEEPSEEK_API_KEY` and any worker API keys at runtime from
environment variables backed by a secret manager. Use different signing keys
for VoxCPM and SoulX; `MODEL_WORKER_TOKEN` is only a migration fallback. `.env`
is for local operation only and must remain untracked. Any key that has appeared
in a chat, log, issue,
shell transcript or Git history is compromised: revoke it and issue a new one
before deployment.

The included gateway container installs the Silero v5 runtime and runs as a
non-root user with a read-only filesystem, dropped capabilities and a bounded
temporary filesystem. Its `.dockerignore` is an allowlist: `.env`, persona
biometrics, model caches, repository history and local virtual environments are
never sent in the Docker build context.

Compose requires a 32-byte-or-longer access or session-signing key because the
process binds `0.0.0.0` inside the container; generate one before
`docker compose up`. Its explicit insecure-private exception is safe only
because the published port is hard-bound to host loopback. Keep it that way for
local use. Before placing a reverse proxy on a container network, remove the
exception and configure its exact IP/CIDR in `ECHOWEAVE_TRUSTED_PROXY_IPS`;
before any direct exposure, use native TLS. URLs using `127.0.0.1`
inside the gateway container point back to that container, not to the host.
Use private Compose service names for model workers, or
`host.docker.internal` on Docker Desktop when the workers run on the host.

Keep `ECHOWEAVE_CONSENT_STATE_PATH` on the persistent `runtime` volume and run
one gateway writer against that JSON state. For multi-worker or multi-replica
gateways, replace it with an external transactional consent/revocation
authority; filesystem snapshots of both the manifest and state can otherwise
roll back together.

The bundled session-token replay cache is likewise process-local. Do not share
one session-signing key across multiple gateway processes until JTI consumption
is backed by one transactional, atomic shared store; otherwise the same
one-time token could be admitted once per process or again after a restart.

## 8. Gateway budgets

The following defaults are exposed in `.env.example`:

| Variable | Default | Purpose |
|---|---:|---|
| `ECHOWEAVE_MAX_ACTIVE_SESSIONS` | 32 | global pending-plus-active admission ceiling |
| `ECHOWEAVE_MAX_WS_MESSAGE_BYTES` | 262144 | maximum control or media WebSocket message |
| `ECHOWEAVE_SESSION_START_TIMEOUT_SECONDS` | 15 | hello-to-start deadline |
| `ECHOWEAVE_RUNTIME_START_TIMEOUT_SECONDS` | 30 | adapter construction budget |
| `ECHOWEAVE_SESSION_IDLE_TIMEOUT_SECONDS` | 300 | no-client-activity deadline after start |
| `ECHOWEAVE_MAX_SESSION_SECONDS` | 1800 | absolute connection lifetime |
| `ECHOWEAVE_WEBSOCKET_SEND_TIMEOUT_SECONDS` | 5 | each outbound socket send |
| `ECHOWEAVE_WEBSOCKET_SHUTDOWN_TIMEOUT_SECONDS` | 5 | connection/session cleanup budget |
| `ECHOWEAVE_OUTBOUND_QUEUE_MAX_MESSAGES` | 128 | outbound queue item ceiling |
| `ECHOWEAVE_OUTBOUND_QUEUE_MAX_BYTES` | 67108864 | outbound queued-byte ceiling |
| `ECHOWEAVE_CONTROL_RATE_PER_SECOND` | 10 | per-IP control refill rate |
| `ECHOWEAVE_CONTROL_RATE_BURST` | 20 | per-IP control burst |

These values prevent unbounded work; they are not a sizing recommendation.
Lower queue byte limits for small instances and high fan-out deployments. Do
not increase them to conceal a slow model, undersized GPU or client that cannot
consume realtime media. Establish limits with the benchmark and at least 30%
capacity headroom.

The reverse proxy must preserve WebSocket upgrade headers and allow normal
sessions to remain open longer than the configured idle interval. It should
still impose its own header/body limits, connection admission controls and TLS
policy. Do not enable proxy buffering for the WebSocket stream.

## 9. Health, readiness and metrics

| Endpoint | HTTP result | Semantics |
|---|---|---|
| `/api/health` or `/api/health/live` | 200 while process responds | liveness only |
| `/api/ready` or `/api/health/ready` | 200 ready, 503 not ready | selected adapters constructed |
| `/api/metrics` | 200 or 403 | bounded JSON metrics snapshot |

All three surfaces use `Cache-Control: no-store`. Docker Compose checks
`/api/ready`, with a startup grace period for model initialization. Kubernetes
should use `/api/health/live` for liveness and `/api/ready` for readiness; never
use readiness as a liveness probe or a transient GPU outage can create a restart
loop.

Readiness has an intentionally narrow scope. Its `checks.runtime` object reports
`readiness_scope="adapter_construction"` and
`dependency_reachability="not_probed"`: constructing an HTTP adapter does not
prove that Qwen, VoxCPM2, SoulX or DeepSeek can complete inference. Add bounded
worker health probes and external synthetic turns in the deployment monitoring
layer.

When `ECHOWEAVE_ACCESS_TOKEN` is configured, `/api/metrics` requires
`Authorization: Bearer <token>`. Without a token, it is available only when the
client, server socket and requested host are all loopback. Do not publish it
directly to the internet. A reverse proxy or collector should authenticate,
scrape the JSON and convert/aggregate it in the monitoring backend without
adding prompts, transcripts, persona IDs or biometric paths as labels.

## 10. Release verification

Before promoting an image:

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .
node --check src/echoweave/web/app.js
node --check src/echoweave/web/mic-worklet.js
node tests/js/test_app_lifecycle.mjs
node tests/js/test_frontend_contract.mjs
node tests/js/test_mic_worklet.mjs
.\.venv\Scripts\python -m pip wheel . --no-deps --wheel-dir dist
.\.venv\Scripts\python scripts\verify_wheel.py dist --expected-version 0.2.0
```

Run `scripts/benchmark_realtime.py` against the candidate stack, compare it with
a same-hardware/same-corpus baseline and exercise worker timeout, slow-client,
disconnect and authorization-revocation paths. See `docs/OPERATIONS.md` for the
SLO and rollback checklist.

Dependency locks, immutable model revisions, byte-reproducible wheel checks and
container/CI pinning are documented in
[`docs/SUPPLY_CHAIN.md`](SUPPLY_CHAIN.md).
