# Outlook / Microsoft Graph OAuth framework (2026-08-25)

## Scope

Add the authentication seam needed before the mailbox provider can be enabled.
`ledgerbridge.mail_oauth` builds Microsoft identity-platform authorization URLs
with PKCE and validates an injected authorization-code token exchange. The
existing `MicrosoftGraphMailProvider` can consume the resulting ephemeral
access-token provider.

## Security boundary

- Network I/O is an injected `OAuthTransport`; no HTTP client is created here.
- Client secrets and refresh tokens are not accepted, logged, or persisted.
- Access tokens are held only in an explicit in-memory `OAuthToken`/provider;
  deployment must inject an approved external token store before real use.
- Authority is pinned to `login.microsoftonline.com`; HTTP redirect URIs are
  limited to loopback.
- PKCE `state`, verifier, challenge, tenant, and client identifiers are bounded.
- Token responses require a Bearer token, bounded expiry (60 seconds to 24
  hours), and a non-empty scope.

## Still disabled

No OAuth client ID, tenant, redirect, secret, token, mailbox, Graph network
request, or production setting was added. `mail_provider=disabled` remains the
default and production mail remains rejected by `Settings`.

## Manual verification

```text
uv run ruff format --check src/ledgerbridge/mail_oauth.py
uv run ruff check src/ledgerbridge/mail_oauth.py
uv run mypy src/ledgerbridge/mail_oauth.py
python -m compileall -q src/ledgerbridge/mail_oauth.py
```

The local self-check constructs a PKCE authorization URL and exercises a fake
injected token transport; it performs no network request.
