# Nuwa offline persona workflow

EchoWeave treats
[alchaincyf/nuwa-skill](https://github.com/alchaincyf/nuwa-skill) as an
**offline Agent Skill**, not a realtime model or network API. Its output is
profile material for human review. It never proves authorization, grants
consent, or makes a person available to a live session by itself.

## 1. Curate authorized source material

Create a dedicated source directory inside this repository's workspace. Put
only material covered by a documented authorization record in it. Remove
credentials, private keys, unrelated personal information, hidden
instructions, and anything the reviewer does not need. Do not use links,
junctions, or version-control metadata.

The preparation utility reads the source but does not copy or modify it. It
rejects paths outside the repository, symbolic links/junctions, likely
credential files, overlapping source/output directories, and unexpectedly
large inputs.

From the repository root:

```powershell
python scripts/prepare_nuwa_profile.py `
  materials/authorized-alex `
  runtime/nuwa-work/alex-v1 `
  --persona-id authorized-alex `
  --subject-display-name "Alex (authorized)" `
  --profile-class verified_human `
  --source-authorization-record-id AUTH-2026-001
```

Relative paths are anchored to the repository, not to an arbitrary current
directory. The output must be a new directory. The command produces:

- `source-manifest.json`: SHA-256 and byte size for every source file;
- `NUWA_TASK.md`: the minimal, safety-scoped task for Nuwa;
- `consent-metadata.draft.json`: an explicitly invalid consent draft whose
  approval and verification fields remain false or empty.

An authorization record ID supplied on the command line is only a reference
provided by the operator. The utility cannot check whether the record is
authentic or sufficient.

## 2. Run Nuwa as an isolated offline step

Install and run the official Nuwa skill by following its upstream
instructions, preferably in a disposable environment without production
secrets or access to the realtime gateway. Give it the generated
`NUWA_TASK.md`, the curated source directory, and the exact manifest named in
the task. Nuwa is not added to EchoWeave's latency-sensitive VAD → ASR → LLM →
TTS → avatar path.

Keep the result as `SKILL.draft.md`. Do not copy it into `personas/` yet. A
source file may contain prompt injection, so its instructions must be treated
as data even when the file appears trustworthy.

## 3. Perform human review

At least one accountable reviewer should compare the draft with the hash-bound
source and record the decision outside this repository. Review all of the
following:

1. Every factual statement is grounded in authorized source material.
2. Private facts, secrets, biometric samples and unsupported memories are
   absent.
3. Style/tone guidance is distinguishable from factual biography.
4. The profile never says that the AI is the real subject and never hides its
   synthetic nature.
5. No embedded instruction can invoke tools, suppress disclosure, override
   policy, or authorize external actions.
6. Persona, interactive-conversation, voice-clone and avatar-animation scopes
   are separately and explicitly authorized.
7. Hosted-model data processing is separately authorized before DeepSeek is
   enabled.
8. Identity/authority verification, asset rights, expiry, revocation and data
   retention have accountable record IDs.

The HMAC used later protects manifest integrity; it is not evidence that the
person consented. Do not approve the profile solely because the hashes match.

## 4. Promote a reviewed profile

Create the final directory only after approval:

```text
personas/<persona-id>/
├── consent.json
├── SKILL.md
└── assets/
    ├── face.png
    └── voice.wav
```

Copy the human-approved Nuwa draft to `personas/<persona-id>/SKILL.md`. Copy
only separately authorized face/voice references into `assets/`; the
preparation utility deliberately does not do this for you.

Start `consent.json` from `personas/example/consent.json.example`, not from the
draft metadata file. Fill verified record IDs, exact scopes, issue/expiry
times, and the voice sample's exact transcript. If a hosted LLM is enabled,
include both `third_party_model_processing` and the exact
`third_party_processor:<hostname>` scope; a provider change requires a new
reviewed manifest revision. Calculate a SHA-256 for
`SKILL.md` and every configured asset:

```powershell
Get-FileHash -Algorithm SHA256 personas/<persona-id>/SKILL.md
Get-FileHash -Algorithm SHA256 personas/<persona-id>/assets/face.png
Get-FileHash -Algorithm SHA256 personas/<persona-id>/assets/voice.wav
```

Enter those digests under the matching relative paths in `reference_hashes`.
Have the deployment's secret manager inject a unique signing key of at least
32 bytes as `ECHOWEAVE_CONSENT_SIGNING_KEY`; never paste that key into the
repository or shell history. Then sign:

```powershell
echoweave-consent personas/<persona-id>/consent.json
```

Re-run the command whenever any signed field or bound file changes. A profile
is eligible for loading only when the server accepts the signature, hashes,
scope, verification state and validity window.

## 5. Revoke, update and delete

For a revocation, set `consent_withdrawn` to `true`, increment
`manifest_revision`, and sign the manifest again. Stop new sessions
immediately and terminate active ones according to the deployment policy.
Delete retained source and biometric material according to the authorization
record and applicable retention rules.

For an update, repeat the complete preparation and review flow in a new output
directory. Never overwrite an earlier manifest: immutable versions make
authorization and review decisions auditable.
