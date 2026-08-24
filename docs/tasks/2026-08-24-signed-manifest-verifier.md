# Phase 3 signed runner manifest verifier

Status: implemented on `ai/chatgpt/phase-3-connector-runner` at `0bf5fea`;
default empty and not deployed.

## Scope

This slice adds a fail-closed Ed25519 verifier for the already-defined
`VerifiedRunnerManifest` composition boundary. The deployment supplies raw
32-byte public keys keyed by an external `key_id`; no key material, real
Connector, OAuth token, or production manifest is stored in the repository.

The signed envelope is canonical UTF-8 JSON with schema version, generation,
key id, runner-only connector declarations, and a base64 signature. Unknown or
duplicate fields, non-canonical encoding, stale generation, unknown keys,
invalid signatures, invalid factories, and capability-incompatible connector
specifications are rejected before construction. Manifest files are read via a
single descriptor with size, regular-file, and replacement checks; POSIX hosts
also use `O_NOFOLLOW`.

`worker.build_worker_manifest()` loads the manifest only when both deployment
paths are explicitly configured. A missing or invalid configuration returns no
manifest, so the existing empty registry and disabled routes remain the safe
default. Production requires an explicit expected generation and runner mode.

## Verification

- `tests/test_signed_manifest.py`: 5 passing tests for valid signatures,
  tampering, canonicalization, duplicate/unknown fields, trusted key lookup,
  and generation pinning.
- Targeted worker/composition/config regression: `41 passed / 1 skipped`.
- Ruff and strict mypy pass for the changed source and full repository.

## Explicit non-goals

This does not create a signing service, rotate or revoke deployment keys,
enable a real manifest, add runner sockets to production Compose, or implement
the trusted principal middleware. Those remain separate review gates.
