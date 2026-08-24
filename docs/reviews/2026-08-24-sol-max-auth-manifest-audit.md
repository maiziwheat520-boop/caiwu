# LedgerBridge narrow security audit: trusted auth + signed runner manifest

Date: 2026-08-24  
Reviewer: Codex / GPT-5.6 Sol Max security sub-agent  
Reviewed branch: `ai/chatgpt/phase-3-connector-runner`  
Reviewed HEAD: `9d4403d`  
Verdict: **CHANGES REQUIRED**

## Executive summary

The cryptographic and composition core is strong: the manifest parser rejects
duplicate and unknown fields, requires one canonical JSON encoding, verifies a
detached Ed25519 signature over the complete declarative payload, reads files
through one descriptor, pins the deployment generation, and permits only the
fixed runner factory in runner mode. The frozen dependency graph resolves only
`cryptography==50.0.0`, with hashed distributions, and the checked build/CI
paths use `uv --frozen`.

Three security defects remain before this gate is suitable for a real
generation or gateway:

| ID | Severity | Result | Root control |
| --- | --- | --- | --- |
| `SOL-A1` | MEDIUM | Actor serialization is ambiguous, admits control text, and can exceed the 200-character persistence boundary | `src/ledgerbridge/auth.py:39-61` |
| `SOL-A2` | LOW | Resolver failure preserves a pre-existing typed principal in ASGI state | `src/ledgerbridge/auth.py:103-114` |
| `SOL-M1` | MEDIUM | Configuration permits the verification-key bundle to share the manifest delivery trust domain | `src/ledgerbridge/config.py:66-79`, `src/ledgerbridge/worker.py:152-160` |

No production route, real manifest generation, gateway, Connector, OAuth
credential, or deployment was enabled during this audit. This was a static,
source-backed review; the reproducer snippets below are minimal deterministic
tests derived from the reviewed code, not claims that production was exercised.

## Scope and security assumptions

Fully reviewed in scope:

- `src/ledgerbridge/auth.py`
- `src/ledgerbridge/signed_manifest.py`
- `src/ledgerbridge/runner_composition.py`
- `src/ledgerbridge/main.py`, limited to `get_authenticated_principal`, its
  upload/async call sites, and body/staging order
- `src/ledgerbridge/config.py`, limited to auth/manifest settings
- `src/ledgerbridge/worker.py`, limited to `build_worker_manifest`
- `pyproject.toml` and the `cryptography`, project, `cffi`, and `pycparser`
  resolution records in `uv.lock`
- `tests/test_auth.py`, `tests/test_signed_manifest.py`, and directly relevant
  route/composition/config tests
- `docs/reviews/2026-08-24-claude-narrow-auth-manifest-audit-request.md`

Supporting source was consulted only to validate effects at the existing
artifact, dispatch, audit, and container-build boundaries. Those components
were not re-audited.

Threat model used for severity:

- Manifest bytes are an untrusted deployment input until signature and policy
  verification succeeds.
- The verification-key bundle is a trust root and must not be writable through
  the manifest delivery authority.
- A trusted gateway resolver may derive provider/subject from externally
  influenced identity claims, but raw HTTP headers are not authoritative.
- ASGI `scope.state` is process-internal, so exploiting a pre-existing typed
  principal requires a faulty or compromised in-process component; this lowers
  `SOL-A2` to LOW.
- Production evidence and Review routes are currently forced off. This reduces
  current reachability, but the reviewed code is itself the proposed gate for
  later enablement, so source-backed gate defects remain findings.

## Findings

### SOL-A1 — MEDIUM — Actor serialization is ambiguous and exceeds its sink boundary

**Taxonomy:** CWE-290 (Authentication Bypass by Spoofing), with resource and
audit-integrity consequences.  
**Confidence:** High.

**Affected source**

- `src/ledgerbridge/auth.py:39-61`
- `src/ledgerbridge/main.py:275-288`
- Route effect: `src/ledgerbridge/main.py:327-364`, `398-404`, `446-463`

**Root cause**

`AuthenticatedPrincipal` validates `provider` and `subject` independently at
64 and 200 characters, then derives the authorization/audit identity with the
unescaped concatenation `f"{provider}/{subject}"`. Neither component reserves
`/`, and the shared text predicate rejects only NUL and Unicode surrogates, not
CR, LF, tab, C1 controls, or leading/trailing whitespace. The combined actor
can reach 265 characters, while existing audit/dispatch/review sinks admit at
most 200.

**Exploit preconditions**

1. A real gateway is later enabled and emits otherwise valid typed principals.
2. Provider or subject is derived from a namespace that can contain `/`,
   control text, or enough characters to exceed the combined sink limit.
3. For cross-principal access, two principals must be mapped to colliding
   component pairs; for the resource effect, one valid principal only needs a
   combined actor longer than 200.

**Minimal reproducer**

```python
common = dict(
    capabilities=frozenset({"evidence:write"}),
    issued_at=now,
    expires_at=now + timedelta(minutes=5),
    policy_generation="policy-1",
)
p1 = AuthenticatedPrincipal(provider="idp/a", subject="b", **common)
p2 = AuthenticatedPrincipal(provider="idp", subject="a/b", **common)
assert p1.actor == p2.actor == "idp/a/b"

too_long = AuthenticatedPrincipal(provider="p" * 64, subject="s" * 136, **common)
assert len(too_long.actor) == 201
assert authorize_principal(
    too_long, "evidence:write", expected_policy_generation="policy-1", now=now
)

control_text = AuthenticatedPrincipal(provider="idp\nforged", subject="user", **common)
assert "\n" in control_text.actor
```

All three objects pass the reviewed principal constructor. The 201-character
actor passes `get_authenticated_principal`. On both evidence-upload paths, body
reading and artifact publication occur before the downstream actor validation
or database width constraint rejects that value. The async status owner check
also compares the flattened actor string, so colliding principals are
indistinguishable at that boundary.

**Impact**

- Two distinct authenticated identities can collapse to the same actor, which
  can misattribute audit events and can satisfy actor-string ownership checks.
- A valid long identity is accepted by the authentication gate but rejected
  after body consumption and artifact publication, allowing repeated orphaned
  artifact creation/capacity consumption within the authenticated upload
  budget.
- Control characters can make exported audit/log representations ambiguous
  even though parameterized database writes prevent SQL injection.

**Fail-closed fix**

Define one canonical identity encoding before authorization returns:

1. Reject all control/format text and leading/trailing whitespace in provider,
   subject, and policy generation.
2. Either reserve and reject the actor delimiter in both components or use an
   unambiguous canonical encoding (for example, structured fields plus a stable
   digest). Do not use raw delimiter concatenation.
3. Enforce the final encoded actor length at 200 or less in
   `AuthenticatedPrincipal.__post_init__`, before any route can read a body.
4. Add collision, 200/201 boundary, CR/LF/tab, and body-not-read/staging-not-
   created regression tests for both synchronous and asynchronous routes.

### SOL-A2 — LOW — Resolver failure preserves a pre-existing typed principal

**Taxonomy:** CWE-287 (Improper Authentication).  
**Confidence:** High.

**Affected source**

- `src/ledgerbridge/auth.py:103-114`
- Acceptance sink: `src/ledgerbridge/main.py:275-288`

**Root cause**

`TrustedPrincipalMiddleware` writes `authenticated_principal` only when the
resolver returns a non-`None` value. If the resolver returns `None` or raises,
the middleware forwards the original scope without deleting an existing value.
A pre-existing `AuthenticatedPrincipal` therefore remains indistinguishable
from a resolver-owned result, including when `auth_provider=trusted_gateway`.

**Exploit preconditions**

An upstream or co-resident ASGI component must be able to prepopulate
`scope["state"]["authenticated_principal"]` with a typed Python object, or an
application integration must accidentally reuse such state. Raw HTTP headers
alone cannot construct this object, so this is not a direct remote-header
bypass.

**Minimal reproducer**

```python
scope = {
    "type": "http",
    "state": {"authenticated_principal": valid_typed_principal},
}
middleware = TrustedPrincipalMiddleware(app, lambda _scope: None)
await middleware(scope, receive, send)
assert scope["state"]["authenticated_principal"] is valid_typed_principal
# get_authenticated_principal(request, trusted_gateway_settings) accepts it.
```

The same behavior occurs when the resolver raises because the exception path
also leaves the existing state untouched.

**Impact**

A faulty or compromised in-process middleware can bypass the intended
resolver-owned identity boundary; resolver outages can also preserve stale
identity state instead of guaranteeing an unauthenticated request.

**Fail-closed fix**

For every HTTP request, copy/create the state map and remove the private
principal slot before invoking the resolver. Reinsert it only when the resolver
returns an exact, valid `AuthenticatedPrincipal`; otherwise forward the scope
with the slot absent. Add tests for resolver `None`, resolver exception, invalid
resolver return type, and both raw-string and typed pre-existing state.

### SOL-M1 — MEDIUM — The key bundle can share the manifest delivery trust domain

**Taxonomy:** CWE-653 (Improper Isolation or Compartmentalization).  
**Confidence:** High for the missing invariant; deployment exploitability
depends on filesystem ownership/mount policy.

**Affected source**

- `src/ledgerbridge/config.py:66-79`
- `src/ledgerbridge/worker.py:152-160`
- Contradicted security claim: `src/ledgerbridge/signed_manifest.py:71-75`

**Root cause**

Settings require both paths and require them to be absolute, but do not require
the verification-key bundle to live outside the manifest delivery directory or
under a distinct immutable trust root. `build_worker_manifest` then loads the
key bundle supplied by that configuration and immediately uses it to verify the
manifest. The verifier's docstring says keys are intentionally not read from
the manifest directory, but no code enforces that property.

**Exploit preconditions**

1. Deployment config points both files into the same writable directory or
   otherwise gives one delivery authority write access to both.
2. An attacker compromises that directory/delivery process.
3. The attacker writes a new Ed25519 public key bundle and a manifest signed by
   the matching private key.

**Minimal reproducer**

```text
/delivery/keys.json       <- attacker public key
/delivery/manifest.json   <- canonical manifest signed by attacker key

LEDGERBRIDGE_RUNNER_VERIFICATION_KEYS_PATH=/delivery/keys.json
LEDGERBRIDGE_RUNNER_MANIFEST_PATH=/delivery/manifest.json
```

Both paths pass current Settings validation. The key has valid base64 and
32-byte length, the signature has valid 64-byte length, and the worker accepts
the pair because the trust root itself was replaced. Generation pinning does
not help if the attacker signs the configured generation.

**Impact**

The manifest-delivery authority can become its own signer and authorize any
declarative connector identity accepted by the fixed runner factory. The
runner-only and fixed-factory controls still prevent Python import-path or
in-process factory injection, but signer separation—the control deciding which
runner connectors are approved—is lost.

**Fail-closed fix**

Make trust-domain separation explicit and machine-verifiable:

1. Require the key-bundle path to be under a deployment-owned immutable trust
   root distinct from the manifest delivery root; reject equal files, equal
   resolved parent directories, and manifest-path ancestry.
2. Validate the invariant again in the deployment manifest/compose checker,
   where mount ownership and read-only flags are visible.
3. On POSIX production, require the key file and its directory chain to be
   regular/non-symlink, non-group/world-writable, and owned by the approved
   deployment identity, or consume it from a separately mounted secret/config
   descriptor.
4. Add a regression test proving same-directory key/manifest configuration is
   rejected before either file is trusted.

## Required attack-path results

### 1. Signature and canonicalization — PASS

- Duplicate keys are rejected at every JSON object depth by
  `object_pairs_hook`.
- Envelope and connector field sets must match exactly; unknown and missing
  fields fail closed.
- The raw file must equal the single sorted, compact, UTF-8 canonical encoding;
  whitespace, key-order, and alternate escape changes fail even when semantic
  JSON is unchanged.
- Invalid base64, non-64-byte signatures, unknown key IDs, non-32-byte public
  keys, altered payloads, unsupported schema versions, and generation mismatch
  all fail closed.
- Ed25519 verification covers connectors, generation, key ID, and schema
  version. The derived manifest digest also covers factory, identity, source,
  execution mode, generation, and schema version.

### 2. File delivery — CHANGES REQUIRED (`SOL-M1`)

- Missing, oversized, directory, and non-regular files fail closed.
- Files are read through one descriptor; before/after `fstat` checks bind the
  consumed bytes to one stable inode/size/time identity. Replacing the pathname
  after open does not change the descriptor being verified.
- POSIX final-component symlinks are rejected with `O_NOFOLLOW`.
- The unresolved defect is trust-root placement: the current configuration can
  select the key bundle from the same delivery directory as the manifest.

### 3. Composition boundary — PASS, conditional on repairing `SOL-M1`

- No manifest field controls a Python import path or factory lookup.
- `factory_id` must equal `ledgerbridge.runner_connector`.
- `RunnerConnectorSpec` requires runner mode even outside production, and the
  manifest parser explicitly reasserts runner-only behavior in production.
- Duplicate `(name, version)` identities are rejected.
- Empty/invalid manifests remain unavailable, and worker startup returns no
  connectors when settings or verification fail.
- A valid signed declaration cannot itself grant database, artifact-path,
  network, or OAuth objects; it only creates the reviewed runner façade. Signer
  authority still has to be isolated as described in `SOL-M1`.

### 4. Principal admission — CHANGES REQUIRED (`SOL-A1`, `SOL-A2`)

- Raw `X-Actor`, reason, Authorization, and certificate-like headers are not
  read by the dependency or middleware implementation.
- Typed principals require the exact `evidence:write` capability, matching
  policy generation, aware timestamps, a positive lifetime no longer than one
  hour, and bounded clock skew from 0 through 300 seconds.
- Missing capability, stale generation, future/expired times outside skew, and
  resolver exceptions fail authorization in the ordinary empty-state case.
- Actor encoding and stale pre-existing state require the fixes above.

### 5. Route ordering — PASS for rejected principals; broken by `SOL-A1` for accepted-but-unsinkable actors

The synchronous and asynchronous evidence routes receive the principal as a
dependency before their manual `_read_bounded_request` call, `begin_handoff`,
publication, importer/dispatch call, or database write. Feature/production
guards also run as decorator dependencies first. Legacy string overrides are
accepted only outside a configured `trusted_gateway` profile, while all current
production routes are forced off.

However, a principal whose flattened actor exceeds 200 is treated as
authenticated and only rejected at a downstream sink after body read and
publication. This is the route-ordering consequence of `SOL-A1`. Existing tests
send a body on an auth failure but do not use a receive stream that proves zero
bytes were consumed for every typed-principal failure; the requested explicit
negative test should be added with the fix.

### 6. Dependency supply chain — PASS

- `pyproject.toml` requires `cryptography>=50,<51`, which cannot select a
  downgraded pre-50 release.
- `uv.lock` has one `cryptography` package record, exactly version `50.0.0`,
  with hashes for the sdist and every accepted wheel.
- The project lock record points to that package; its platform dependency is
  the separately locked `cffi` record (and locked `pycparser` where applicable).
- Both application Dockerfiles and hosted CI use `uv sync --frozen`; container
  installation of `uv` itself is hash-checked and binary-only.

The security claim applies only to frozen-lock builds. Installing the project
directly from `pyproject.toml` without `uv.lock` may select a later 50.x release
and would no longer reproduce this audited dependency set.

## Non-findings and deployment-owned gates

The following remain explicitly outside this implementation verdict and must
stay disabled until separately reviewed: certificate/SAN mapping, OIDC/JWKS
verification, gateway network topology, key rotation and revocation workflow, a
real signed generation, real Connector/OAuth credentials, merge, and production
enablement. Their absence is not counted as a defect in this narrow slice.

No secret, private signing key, credential, live gateway metadata, or production
data was read or created by this audit.

## Required closure evidence

Before requesting a follow-up narrow audit:

1. Add regression tests for actor collisions, total actor length 200/201,
   CR/LF/tab and whitespace, and zero body/staging activity on every rejection.
2. Add middleware tests proving resolver `None`/exception removes raw and typed
   pre-existing principal state.
3. Add settings/deployment tests proving trust-root and manifest-delivery paths
   cannot share a writable trust domain.
4. Re-run focused tests, full frozen tests, static checks, dependency audit, and
   an isolated Hermes/container verification without installing any real key,
   manifest, gateway, Connector, or credential.

## Re-review of remediation commit `7a73933`

Re-review date: 2026-08-24

Range: `9d4403d..7a739336cc030861abe353adb2ac1fc00f608bd1`

Final re-review verdict: **CHANGES REQUIRED**

The remediation closes `SOL-A1`, but `SOL-A2` remains bypassable through the
resolver's unsanitized view of the original scope and `SOL-M1` still separates
path topology rather than filesystem authority. Closure evidence is also
incomplete, and both hosted CI runs for the exact commit failed their quality
job.

### SOL-A1 — CLOSED

Exact evidence:

- `src/ledgerbridge/auth.py:41-56` validates provider, subject, and policy text,
  rejects `/` in both actor components, and rejects a combined actor over 200
  characters during `AuthenticatedPrincipal` construction.
- `src/ledgerbridge/auth.py:123-132` rejects leading/trailing whitespace and
  every non-printable character before the route dependency can accept the
  principal.
- `tests/test_auth.py:95-117` covers delimiter, NUL/newline, whitespace, and the
  exact 200/201 actor boundary.

This removes the collision and accepted-but-unsinkable actor paths. Because a
201-character principal can no longer be constructed, it cannot reach either
manual body reader or artifact publication. No residual `SOL-A1` exploit was
found in the reviewed diff.

One requested defense-in-depth test is still absent: the remediation does not
add a receive-stream sentinel that proves zero body bytes and zero staging
activity for each typed-principal rejection on both upload routes. Static route
ordering remains correct, so this is recorded as a closure-evidence gap rather
than reopening `SOL-A1`.

### SOL-A2 — NOT CLOSED (LOW)

Exact source: `src/ledgerbridge/auth.py:110-119`.

The middleware copies state and removes `authenticated_principal` from the
copy, but calls `self.resolver(scope)` before assigning that sanitized copy back
to `scope["state"]`. The resolver therefore still sees the original stale
principal and can return it. The subsequent `isinstance` check accepts that
same typed object and reinstalls it.

Executed minimal reproducer against `7a73933`:

```python
p = valid_typed_principal
scope = {"type": "http", "state": {"authenticated_principal": p}}
resolver = lambda supplied: supplied["state"]["authenticated_principal"]
await TrustedPrincipalMiddleware(app, resolver)(scope, receive, send)
assert scope["state"]["authenticated_principal"] is p
```

The assertion evaluated `True`. Existing tests cover resolver `None`, an
exception, and an invalid return, but none makes the resolver inspect the stale
scope value, so all tests pass while this attack path remains.

Exploit precondition and impact are unchanged from the original LOW finding: a
faulty or compromised in-process component must prepopulate scope state and the
resolver must consult that slot. This is not a raw remote-header bypass, but it
violates the stated resolver-owned invariant.

Required fail-closed correction: install the sanitized state into the scope
before invoking the resolver, or pass the resolver a sanitized scope copy. Add
a regression resolver that attempts to echo the old typed principal and prove
the request remains unauthenticated.

### SOL-M1 — NOT CLOSED (MEDIUM)

Exact source: `src/ledgerbridge/config.py:70-82`.

The new validator rejects the same file, equal parent directories, and ancestor
or descendant directories. It does not establish different trust domains:
sibling directories owned or writable by the same manifest-delivery identity
are explicitly accepted. No changed deployment checker, mount declaration,
owner/mode check, or immutable trust-root setting proves that the keys directory
has a different write authority.

Executed acceptance reproducer against `7a73933`:

```python
root = temporary_path / "attacker-controlled-delivery"
settings = Settings(
    database_url="sqlite+pysqlite:///:memory:",
    artifact_root=temporary_path,
    runner_manifest_path=root / "manifest" / "manifest.json",
    runner_verification_keys_path=root / "keys" / "keys.json",
)
assert settings.runner_manifest_path.parent.parent == (
    settings.runner_verification_keys_path.parent.parent
)
```

Construction succeeded and the assertion evaluated `True`. A writer controlling
that common delivery root can still replace both the key bundle and the
manifest and self-sign the configured generation. Path siblinghood is not an
authorization boundary.

There is also a validation/use gap: the validator compares `Path.resolve()`
results but retains the original configured paths, while the later file opener
only applies `O_NOFOLLOW` to the final component. A writable parent-directory
symlink can therefore be redirected after settings validation and before
`build_worker_manifest` reads the files.

Required fail-closed correction: bind the key bundle to a separately configured
deployment-owned trust root and verify the actual deployment authority—at
minimum immutable/read-only mount topology plus approved POSIX owner and
non-group/world-writable directory chain. The deployment checker must reject a
manifest delivery identity that can write either the key file or any parent in
its resolution path. Add a test that models the same writer controlling two
sibling directories; same/nested-directory tests alone do not close `SOL-M1`.

### Closure evidence observed

- Focused frozen tests independently rerun:
  `tests/test_auth.py`, `tests/test_config.py`, `tests/test_upload_route.py`, and
  `tests/test_signed_manifest.py` — **87 passed, 1 warning**.
- Those green tests do not cover the two residual reproducers above.
- The remediation report claims a full local Windows suite of 314 passed / 149
  skipped, but this re-review did not rerun the entire suite.
- No exact-commit disposable Hermes replay is claimed in the remediation
  report.
- Hosted push run `32702484291` and pull-request run `32702487780` for
  `7a739336cc030861abe353adb2ac1fc00f608bd1` both completed **failure**.
  `secrets` and `compose` succeeded; `quality` failed at
  `uv run --frozen --extra dev ruff format --check .`.

### Final release decision

`7a73933` must not be treated as a passing auth/manifest gate. Keep the real
generation, gateway, Connector/OAuth integration, merge, and production
enablement disabled. A follow-up review should use a new exact commit that
sanitizes scope before resolver execution, proves key-custody authority rather
than directory shape, adds the missing attack-path tests, and has green hosted
quality/secrets/compose evidence.
