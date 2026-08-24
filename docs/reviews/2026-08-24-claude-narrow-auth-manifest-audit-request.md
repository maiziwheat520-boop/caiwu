# Claude narrow audit request: trusted auth + signed runner manifest

Status: handoff packet prepared; no Claude verdict exists yet.

## Review target

Audit only the new security gates on branch
`ai/chatgpt/phase-3-connector-runner`, commits `7ff728f..9fccd6a` plus the
dependency/security follow-ups `6f963c1`, `9eb7de1`, and `17800dd`:

- `src/ledgerbridge/signed_manifest.py`
- `src/ledgerbridge/runner_composition.py`
- `src/ledgerbridge/auth.py`
- `src/ledgerbridge/main.py` (`get_authenticated_principal` seam only)
- `src/ledgerbridge/config.py` (manifest/auth settings only)
- `src/ledgerbridge/worker.py` (`build_worker_manifest` only)
- `pyproject.toml`, `uv.lock`
- `tests/test_signed_manifest.py`, `tests/test_auth.py`

Do not re-audit the already reviewed ledger, artifact, importer, dispatch,
ReviewItem, or runner protocol implementation except where these gates cross
their boundary. Do not use production data, credentials, gateway certificates,
OAuth, or a real Connector.

## Required attack paths

1. **Signature/canonicalization**: duplicate JSON keys, unknown fields, changed
   whitespace/order, invalid base64, wrong key id, wrong public-key length,
   altered payload, schema downgrade/upgrade, generation mismatch, and a valid
   signature over a capability-inflating connector declaration.
2. **File delivery**: missing/partial/oversized/regular-file violations,
   symlink behavior on POSIX, replacement between stat/read/stat, and whether
   key bundles can be selected from the manifest directory itself.
3. **Composition boundary**: dynamic factory/import path injection, duplicate
   connector identities, in-process production mode, API/worker generation
   disagreement, empty/invalid manifest readiness, and whether a signed
   declaration can grant network/database/artifact/OAuth authority.
4. **Principal admission**: raw `X-Actor`, `X-Reason`, Authorization, or
   certificate-like headers; resolver failure; pre-existing raw
   `request.state`; missing capability; stale/future/expired timestamps;
   policy-generation mismatch; clock-skew extremes; overlong/control-text
   provider and subject; actor derivation and audit leakage.
5. **Route ordering**: every auth failure must occur before body read,
   `ArtifactStore.begin_handoff()`, staging, importer invocation, or database
   writes. Confirm typed principal is used for both synchronous and async
   routes and that legacy test overrides do not create a deployable fallback.
6. **Dependency supply chain**: confirm `cryptography==50.0.0` is the resolved
   audited release and that no downgraded transitive wheel is accepted.

## Expected verdict format

Return `PASS`, `CHANGES REQUIRED`, or `BLOCKED`, with each finding containing:
severity, exact file/line, exploit precondition, minimal reproducer, impact,
and a concrete fail-closed fix. Distinguish implementation defects from
deployment-owned open gates. Do not recommend enabling a real generation or
production gateway as part of this audit.

## Local evidence already available

- Signed manifest tests: `13 passed`; module coverage 100%.
- Auth tests: 9 passed; module coverage 100%.
- Windows full regression: `307 passed / 149 skipped / 1 warning`.
- Ruff, strict mypy, and diff check pass.
- Hosted CI push `32696893613` and PR `32696888144` are green for `9fccd6a`.
- No signing key, manifest, gateway certificate, token, OAuth credential, or
  production configuration was added to the repository.

## Explicit open gates (not findings by themselves)

Certificate/SAN mapping, OIDC/JWKS verification, key rotation/revocation,
gateway deployment topology, a real signed generation, real Connector/OAuth,
merge, and production enablement remain disabled and require separate review
and authorization.
