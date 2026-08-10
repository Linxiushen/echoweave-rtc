# Cloudflare public demo

This runbook exposes only the EchoWeave gateway on loopback through a
Cloudflare Quick Tunnel. It is suitable for a temporary, explicitly disclosed
fictional `demo` persona. It is not a production deployment and it must not be
used to publish real-person media, private transcripts, model-worker ports or
long-lived secrets.

GitHub Pages cannot run the Python gateway, WebSocket sessions or GPU workers.
GitHub remains the source and CI host; Cloudflare provides the temporary HTTPS
and WSS edge.

## Provider capability boundary

An OpenAI-compatible gateway can be used only for the protocol it actually
documents and passes in qualification tests. The 2026-08-10 operator smoke test
qualified ShrimpAI model `step-3.7-flash` for `/v1/models` and terminated
streaming `/v1/chat/completions`; the sanitized result is recorded in
[`qualification/shrimpai-20260810.json`](qualification/shrimpai-20260810.json).
No image generation, video generation, ASR, TTS, realtime media or EchoWeave
avatar endpoint was qualified.

Therefore:

- `openai_compatible` may supply the LLM stage;
- Qwen3-ASR remains a separately qualified local/private worker;
- VoxCPM2 remains a consent-verifying private TTS worker;
- SoulX remains a consent-verifying private avatar worker;
- browser TTS and client lipsync are used by this public demo;
- no real-person reference voice or face is sent to the hosted LLM gateway.

## Install

Install Cloudflare's official tunnel client and the project environment:

```powershell
winget install --id Cloudflare.cloudflared -e
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
```

The launcher also accepts the official Windows release binary at
`runtime/tools/cloudflared-windows-amd64.exe`. Keep downloaded tools under
`runtime/`; the directory is excluded from Git and Docker build context.

The script fails closed if port `8765` is already in use. Use another `-Port`
instead of stopping an unrelated process.

## Start a no-cost demo

```powershell
.\scripts\start_cloudflare_demo.ps1
```

The script creates a random, run-scoped application access token, starts both
processes in the background, waits for the public health check, and prints an
`ACCESS_URL`. The URL may be reused while that demo run is active; it stops
working when the gateway is stopped. During launch, the token exists briefly in
launcher memory and standard output, then remains in the gateway process
environment and the URL fragment. It is not written to the status file or Git.
Treat terminal output as sensitive: do not redirect or paste it into logs,
issues, chat or support tickets.

## Start with a hosted LLM

Inject a separately created, limited and revocable provider key into the
current process. Never put the key in a command checked into source control or
in `.env.example`.

```powershell
$env:OPENAI_COMPAT_API_KEY = "<secret>"
$env:OPENAI_COMPAT_BASE_URL = "https://shrimpai.cc/v1"
$env:OPENAI_COMPAT_MODEL = "<exact-account-model-id>"
$env:OPENAI_COMPAT_MAX_TOKENS = "800"
.\scripts\start_cloudflare_demo.ps1 -UseOpenAICompatible
```

Use a short expiry, a small total quota and a model allowlist on the provider
key. A public demo must not use an unlimited account key.

## Verify and stop

Check the public health endpoint and then test a synthetic text turn in the UI.
The model-worker ports `8001`, `8002` and `8003` must remain unreachable from
the internet.

```powershell
.\scripts\stop_cloudflare_demo.ps1
```

Quick Tunnel hostnames are random, have no SLA and stop working when either
local process or the computer stops. A durable deployment requires a named
Cloudflare Tunnel, a controlled hostname, Cloudflare Access, an exact
`ECHOWEAVE_ALLOWED_ORIGINS`, application session authentication and supervised
gateway/tunnel services. Cloudflare Access is defense in depth and does not
replace EchoWeave's persona-scoped authorization or consent revocation.
