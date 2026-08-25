# EML attachment intake boundary (2026-08-25)

## Scope

`ledgerbridge.mail_eml.parse_eml` parses an exported RFC 5322 message with
bounded raw bytes, body characters, attachments, filenames, media types, and
decoded content. Inline parts are ignored; normal attachments become bounded
`MailAttachment` values.

The synthetic gateway exposes `POST /v1/intake/eml`. It requires an explicit
`X-LedgerBridge-Entity-Ref`, derives a deterministic source event/evidence
identity from `Message-ID`, and reuses the same candidate-intent output as the
JSON route. A message with no file attachment uses a bounded RFC822 evidence
representation so financial text cannot create a candidate without evidence.

## Safety boundary

- No Outlook/Graph network call or OAuth token is involved.
- No original email or attachment is written to disk, PostgreSQL, or the
  ArtifactStore; candidates live only in the process memory of the demo.
- `writes_posting` remains `false` and the route is loopback-only.
- Entity scope is caller-supplied and mandatory; production must replace this
  with an authenticated grant, not a header.

## Manual verification

```text
uv run ruff format --check src/ledgerbridge/mail_eml.py scripts/r1_synthetic_data_gateway.py
uv run ruff check src/ledgerbridge/mail_eml.py scripts/r1_synthetic_data_gateway.py
uv run mypy src/ledgerbridge/mail_eml.py scripts/r1_synthetic_data_gateway.py
python -m compileall -q src/ledgerbridge/mail_eml.py scripts/r1_synthetic_data_gateway.py
```

The focused manual replay posts a multipart EML fixture and receives `201`
with `triage_action=CANDIDATE`, a stable source event UUID, attachment digest,
and `writes_posting=false`.
