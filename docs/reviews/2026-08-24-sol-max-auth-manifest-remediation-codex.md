# Sol Max trusted-auth and signed-manifest remediation

Date: 2026-08-24  
Branch: `ai/chatgpt/phase-3-connector-runner`  
Base audit: [`2026-08-24-sol-max-auth-manifest-audit.md`](2026-08-24-sol-max-auth-manifest-audit.md)

## Outcome

The initial Sol Max verdict was **CHANGES REQUIRED**: two MEDIUM findings and
one LOW finding. All three findings now have fail-closed code and regression
coverage. This report records the implementation and local evidence; a fresh
Sol Max recheck and hosted Linux/PostgreSQL CI remain release gates. No
production, Hermes production, real gateway, real Connector, OAuth credential,
or signing key was enabled or created.

## Finding response

| Finding | Remediation | Evidence |
| --- | --- | --- |
| `SOL-A1` MEDIUM — ambiguous/control-text/overlong actor | Principal text now rejects non-printable format/control characters and leading/trailing whitespace. Provider and subject reserve `/`, and the canonical `provider/subject` actor is bounded to the 200-character persistence limit before route admission. | `tests/test_auth.py`: delimiter/control/whitespace rejection and exact 200/201 boundary tests. Existing route dependency tests remain green. |
| `SOL-A2` LOW — stale typed principal survives resolver failure | HTTP middleware copies state, removes the private principal slot before resolution, and reinstalls it only for an exact `AuthenticatedPrincipal`. Resolver `None`, exceptions, and invalid return objects all leave the request unauthenticated. | `tests/test_auth.py`: stale typed state, stale raw state, resolver exception, `None`, and invalid return tests. |
| `SOL-M1` MEDIUM — key bundle shares manifest delivery trust domain | Settings resolve both paths and reject the same file, same parent, or nested/ancestor deployment directories. Sibling manifest and key directories remain valid. | `tests/test_config.py`: same-directory and nested-directory rejection plus sibling-directory acceptance. |

## Changed files

- `src/ledgerbridge/auth.py`
- `src/ledgerbridge/config.py`
- `tests/test_auth.py`
- `tests/test_config.py`

The implementation remains disabled by default: `auth_provider=disabled`, an
empty runner registry without explicit verified deployment paths, and all real
Connector/OAuth paths closed.

## Verification

Passed locally with the frozen environment:

- `uv lock --offline`
- `uv run --frozen --extra dev ruff format ...`
- `uv run --frozen --extra dev ruff check ...`
- `uv run --frozen --extra dev mypy src tests`
- Focused tests: `74 passed, 1 warning`
- Full Windows suite: `314 passed, 149 skipped, 1 warning`
- `git diff --check`

The skipped cases are platform- or PostgreSQL-gated (POSIX symlink/runner,
PostgreSQL integration, and Linux-only durability checks); hosted CI is required
for the corresponding Linux/PostgreSQL gate. A disposable Hermes replay is not
claimed by this report until it is run against this exact commit; no production
Hermes resources were touched.

## Remaining release gates

1. Hosted `secrets`, `quality`, and `compose` workflows must pass for this
   commit.
2. Sol Max must re-read the post-fix tree and return `PASS` with exact evidence.
3. Key custody, gateway certificate/OIDC mapping, rotation/revocation, a real
   signed generation, real Connector registration, and production enablement
   remain separate authorized tasks.

