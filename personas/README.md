# Persona registration

The gateway never accepts an arbitrary face or voice upload as a runnable
persona. A real-person profile must be prepared offline and placed in:

```text
personas/<persona_id>/
├── consent.json
├── SKILL.md
└── assets/
    ├── face.png
    └── voice.wav
```

`SKILL.md` may be produced by
[alchaincyf/nuwa-skill](https://github.com/alchaincyf/nuwa-skill), then reviewed
by a human. Nuwa is an offline research/distillation method, not a model in the
realtime path.

For a real person, `consent.json` must:

- record separate consent for persona, voice and avatar use;
- include `third_party_model_processing` and the exact signed
  `third_party_processor:<hostname>` scope before using any hosted LLM;
- include an expiry and revocation state;
- include identity/authority verification record identifiers;
- bind reference files by SHA-256;
- be HMAC-signed by the deployment operator.

All on-disk profiles, including original fictional characters, require a
signature, a rights record, complete scope and SHA-256 binding for every used
asset. Only the built-in abstract `demo` profile bypasses this registration
path. `profile_class` is an operator-reviewed classification, not proof by
itself that an image or voice is fictional.

Copy `example/consent.json.example`, calculate the hashes, set
`ECHOWEAVE_CONSENT_SIGNING_KEY`, then run:

```powershell
echoweave-consent personas/my-persona/consent.json
```

The HMAC is an integrity control for the local manifest. It is not, by itself,
proof that the human consented; the referenced verification record and your
operational process provide that proof.

Every changed manifest must use a higher `manifest_revision`. Once a signed
manifest sets `consent_withdrawn: true` or `consent_granted: false`, that
`consent_id` is permanently tombstoned. A later, genuinely new authorization
must use a new consent ID. Keep `ECHOWEAVE_CONSENT_STATE_PATH` on durable private
storage; multi-replica deployments need an external monotonic consent store.
