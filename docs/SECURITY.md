# Consent and synthetic-media safety

EchoWeave is designed for authorized digital twins, fictional characters and
clearly disclosed synthetic media. It must not be used to impersonate a person,
mislead an audience, bypass identity checks, commit fraud, harass a subject or
produce deceptive disinformation.

## Required human inputs and authorization

The demo persona is fictional and needs no biometric input. A real-person
persona needs subject- or rights-holder-approved source material. At minimum:

- a reviewed persona profile or `SKILL.md` derived only from authorized sources;
- an authorization record that separately covers interactive conversation,
  persona behavior, voice cloning, avatar animation and retention;
- a face reference and a clean voice reference with its exact transcript;
- verification/reviewer record IDs, issue and expiry times, revocation channel;
- SHA-256 binding for every loaded file and a server-side signature;
- `third_party_model_processing` plus an exact
  `third_party_processor:<hostname>` scope when a hosted LLM receives the
  profile or conversation.

Collect only what is required for the authorized purpose. Do not scrape a
stranger, infer consent from public availability, or treat ownership of a media
file as permission to clone its subject. Voice and face data are sensitive
biometric material in many jurisdictions; operators are responsible for the
applicable legal basis, notices, access controls, retention and deletion.

Nuwa is an offline drafting tool. Its output must be reviewed by an accountable
human and cannot prove identity, authority or consent. The HMAC signature proves
that a manifest was not changed after signing; it does not prove the underlying
authorization is truthful.

## Enforced in code

- The realtime endpoint accepts an allowlisted `persona_id`, not arbitrary live
  face or voice uploads.
- Every non-demo profile requires signed scopes for conversation, persona,
  voice cloning and avatar animation. Any hosted LLM additionally requires both
  `third_party_model_processing` and an exact processor-host scope such as
  `third_party_processor:api.deepseek.com`. Changing providers therefore
  requires a newly reviewed and signed manifest revision.
- Real-person profiles require a verification record ID, expiry, non-withdrawn
  status and SHA-256 binding for every reference asset.
- Each session keeps an immutable authorization snapshot. Exact Nuwa profile,
  face and voice bytes are hash-verified in one read; active sessions never
  consume later replacements from mutable persona paths.
- Signed highest revisions and irreversible withdrawal tombstones are stored in
  the HMAC-authenticated `ECHOWEAVE_CONSENT_STATE_PATH`.
- Authorization is revalidated during a live session. Failure advances the
  generation, clears playout and prevents stale model output from continuing.
- Session start requires `ai_disclosure_ack=true`; the protocol advertises
  `identity.ai_disclosure`.
- The UI keeps an `AI 数字分身 / SYNTHETIC MEDIA` disclosure visible, and the
  SoulX bridge burns `AI DIGITAL TWIN` into every returned MP4 segment.
- The model prompt cannot claim to be the real person.
- Non-loopback use requires authentication plus HTTPS/WSS. Clear-text HTTP/WS
  fails before session admission; only exact trusted proxy IPs/CIDRs may rewrite
  the ASGI scheme. Origins, connection counts, control rate, audio rate and
  message sizes are bounded.
- WebSocket output is single-writer and bounded by bytes/messages/send time.
  Slow clients are disconnected instead of accumulating sensitive media.
- Secrets are read only from server environment variables.

## Secret handling

The example environment file contains placeholders only. Real values for
`DEEPSEEK_API_KEY`, `OPENAI_COMPAT_API_KEY`, `ECHOWEAVE_ACCESS_TOKEN`,
`ECHOWEAVE_CONSENT_SIGNING_KEY`, `ECHOWEAVE_SESSION_SIGNING_KEY`,
`VOXCPM_WORKER_TOKEN`, `SOULX_WORKER_TOKEN` and worker API keys must be injected
at runtime from a secret manager or protected server environment. Use separate
VoxCPM and SoulX keys; `MODEL_WORKER_TOKEN` exists only as a migration fallback.

Do not put secrets in source code, persona files, browser JavaScript, URL query
strings, container images, benchmark reports or metric labels. The UI accepts a
local `#token=` fragment, copies it to memory and removes it from the address
bar; use a short-lived/session-scoped credential in production rather than a
long-lived infrastructure token.

Every non-demo persona requires a short-lived signed session token whose
subject, persona set and negotiated capabilities are explicit. Tokens are
one-time-use at session admission, and their `exp` is also a hard deadline for
the active socket. The shared `ECHOWEAVE_ACCESS_TOKEN` is demo-only and
configuration fails closed if a non-demo persona is allowlisted without
`ECHOWEAVE_SESSION_SIGNING_KEY`. Worker credentials never authorize a clone
directly: the gateway replaces them with per-request consent assertions bound
to the manifest revision, reference-media hashes, scope, audience, expiry and
replay-protected JTI.

Any key shown in a chat, issue, terminal log, screenshot or Git history is
exposed. Revoke it immediately, generate a replacement and review access logs.
Deleting the visible string is not rotation and does not make the old key safe.

## Health, metrics and logs

`/api/health` is safe liveness only. `/api/ready` returns adapter-construction
state and opaque error references, not endpoints or exception strings.
`/api/metrics` is Bearer-protected when an access token is configured and is
otherwise limited to fully loopback requests.

Never use session IDs, persona IDs, prompts, transcripts, exception messages,
tokens or biometric paths as metric labels. Production logs should use fixed
error classes and opaque correlation IDs. Raw audio/video, reference material
and transcripts must not be recorded by default. Diagnostic capture requires a
separate, explicit, time-bounded authorization and deletion policy.

## Operator responsibilities

The software cannot determine whether an authorization document is truthful.
The operator must perform identity/authority verification, maintain revocation,
honor deletion, minimize stored biometric data and keep an audit trail. Add
metadata provenance to exported files; a fixed visual watermark is not a
substitute for C2PA-style export provenance.

The bundled consent state is designed for one gateway process and survives
ordinary restarts. It is not a transparency log or multi-replica database.
Rolling back both manifest and state, sharing the JSON file between writers or
losing the volume defeats its monotonic guarantee. Use an external authoritative
consent/revocation store with transactional monotonic updates for multiple
replicas or higher-assurance deployments. A withdrawn `consent_id` is never
reusable; a genuinely new authorization needs a new ID.

Session and worker JTI replay caches are also in-process bounds, not distributed
authorities. A production multi-process or multi-replica deployment must use an
atomic shared replay store before sharing signing keys; process restarts should
otherwise be treated as invalidating all outstanding one-time tokens.

## Deployment controls

- HTTPS/WSS, exact allowed origins and per-tenant short-lived authorization;
- proxy headers disabled by default and trusted only from exact peer IPs/CIDRs;
- isolated private model workers with authentication and no arbitrary outbound
  network access;
- independent GPU concurrency ceilings, admission limits and tested timeouts;
- encrypted storage/backups for consent state and authorized biometric assets;
- no default recording and a documented retention/deletion workflow;
- tool calls only through an allowlisted broker; external side effects require
  explicit user confirmation and idempotency keys;
- incident response that stops new sessions whenever authorization or synthetic
  disclosure integrity is uncertain.
