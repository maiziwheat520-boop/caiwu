# Isolated Outlook staging replay (2026-08-25)

## Scope

`scripts/r1_staging_graph_replay.py` is the first usable Graph-to-demo path.
It reads a short-lived access token from process environment, fetches one
bounded inbox page, maps subject/body preview/attachments into the synthetic
gateway contract, and returns candidate projections. It is intentionally
separate from the production provider and persistence paths.

## Explicit enablement

The script exits without network access unless
`LEDGERBRIDGE_STAGING_NETWORK=1` is present. A staging operator must also set
exactly one of `LEDGERBRIDGE_STAGING_ACCESS_TOKEN` (short-lived, process-only)
or `LEDGERBRIDGE_STAGING_CREDENTIAL_TARGET` (a generic credential target in
Windows Credential Manager), plus `LEDGERBRIDGE_STAGING_MAILBOX` and
`LEDGERBRIDGE_STAGING_ENTITY_REF`. The optional
`LEDGERBRIDGE_STAGING_GATEWAY_URL` defaults to the loopback gateway.

The Credential Manager provider is read-only; it never creates, exports, or
transfers the credential. Do not put a token in `.env`, Git, task notes, shell
history, or captured logs. A long-lived credential remains on the local machine
and does not authorize production enablement.

To use the existing Google Drive credentials directory instead, set
`LEDGERBRIDGE_STAGING_CREDENTIAL_FILE` to a file under
`G:\我的云端硬盘\凭据\` and add exactly one line in that external file:

```text
LEDGERBRIDGE_STAGING_ACCESS_TOKEN=<token held only in the credentials file>
```

The provider rejects files outside that directory and never writes the file.

## Bounds and safety

- Graph host is pinned to `graph.microsoft.com`; one page, five messages.
- Each attachment/evidence item is limited to 1 MiB.
- The gateway remains loopback-only, in-memory, and
  `writes_posting=false`.
- The staging payload sets its synthetic admission watermark to the source
  `receivedDateTime`, so historical test mail is not silently dropped by the
  demo activation boundary. This caller-controlled field is only valid for
  this synthetic launcher; production must use a configured deployment
  watermark.
- No refresh tokens, database writes, artifact writes, audit events, or
  production mailbox settings are enabled.
- Production Outlook/Graph enablement remains blocked by the existing config
  and deployment gates; this target is only a staging source for bounded replay.

## Verification

```text
uv run --frozen --extra dev python scripts/r1_staging_graph_replay.py --check
uv run ruff format --check src/ledgerbridge/mail_collector.py scripts/r1_staging_graph_replay.py scripts/r1_synthetic_data_gateway.py
uv run ruff check src/ledgerbridge/mail_collector.py scripts/r1_staging_graph_replay.py scripts/r1_synthetic_data_gateway.py
uv run mypy src/ledgerbridge/mail_collector.py scripts/r1_staging_graph_replay.py scripts/r1_synthetic_data_gateway.py
uv run ruff check src/ledgerbridge/mail_credentials.py
uv run mypy src/ledgerbridge/mail_credentials.py
python -m compileall -q src scripts
```

No real token or mailbox was used during repository verification.

## Short operator flow

After adding exactly one `LEDGERBRIDGE_STAGING_ACCESS_TOKEN=...` line to the
external credentials file, run `powershell -ExecutionPolicy Bypass -File
scripts/r1_staging_run.ps1` from the repository root. The wrapper supplies the
approved staging mailbox/entity defaults, starts a loopback gateway when one is
not already listening, and tears down only the gateway process it started. It
does not display or persist the token. Production mail, posting, and durable
artifact writes remain disabled.

If the token does not exist, use the free Graph Explorer path: sign in at
<https://developer.microsoft.com/en-us/graph/graph-explorer>, choose **Modify
permissions**, consent to delegated `Mail.Read`, and copy the short-lived
value from the **Access token** tab into the external credentials file. No
Azure subscription or new app registration is needed. Microsoft documents
this tab and consent flow at
[Graph Explorer features](https://learn.microsoft.com/en-us/graph/graph-explorer/graph-explorer-features).
The optional device-code helper is retained for a future separately approved
public-client registration, not required for this demo.

## IMAP staging alternative

The provider-neutral boundary also includes `ImapMailProvider`. The verified
no-new-app-registration path is the dedicated 163 mailbox: the wrapper defaults
to `imap.163.com:993` TLS, password-mode auth, and the `authorization_code`
key in the external `hermes-163-mail.env` file. Both the mailbox and Hermes
have been login-tested without reading or modifying messages. The adapter is
read-only, bounded to five messages, and posts only to the loopback synthetic
gateway.

Outlook.com remains supported only through OAuth2. Place a token with the
delegated scope `https://outlook.office.com/IMAP.AccessAsUser.All` under
`LEDGERBRIDGE_STAGING_IMAP_ACCESS_TOKEN` and run
`scripts/r1_staging_run.ps1 -ImapHost outlook.office365.com -ImapAuth xoauth2`.
A Graph `Mail.Read` token is not valid for IMAP. Outlook basic authentication
was experimentally rejected with `Basic authentication is disabled`.

The staging helper `scripts/r1_imap_oauth_login.ps1` obtains this token with
PKCE using Thunderbird's documented public client configuration, without a
paid Azure subscription. It validates state, stores only the access token in
the external credentials file, and discards refresh tokens. This is not a
production identity decision; production must use its own reviewed app.

For a no-registration fallback, Thunderbird can perform the Microsoft OAuth
login and synchronize the Inbox locally. `scripts/r1_staging_mbox_replay.py`
reads that mbox read-only and never handles the OAuth token:

```text
pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/r1_staging_run.ps1 -Transport mbox -MboxPath <Thunderbird INBOX mbox>
```

Use `scripts/r1_set_staging_credential.ps1` when the operator wants a hidden
local prompt instead of manually editing the external credentials file. The
secret is not a command-line argument, is not echoed, and is written only to
the approved credentials directory.

Forwarded financial ZIPs remain a review boundary. Run
`scripts/r1_staging_financial_zip_check.py --message-index 2
--visible-password-input` to enter each password visibly in the local terminal;
omit the visible-input flag for a hidden prompt. The helper verifies and reads
entries only in memory, enforces per-entry/archive decompression limits, and
discards the password and bytes after each archive; it does not decrypt
automatically, write artifacts, or create postings.
