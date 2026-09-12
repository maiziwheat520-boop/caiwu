# Local import (single-user mode)

What can be imported on one machine, what cannot, and why. Local mode changes
who is admitted and over which socket; it does not change what a financial fact
is, and it does not lower a single gate that guards the deployed database.

Read this before assuming a step is missing: two of the import paths in this
repository deliberately refuse to run outside production, and one of those
refusals is not a configuration flag but a proof about a real backup.

## The two import families

| | Controlled review batch | Registered-account statement cutover |
|---|---|---|
| Code | `src/ledgerbridge/controlled_import.py` | `src/ledgerbridge/mybank_statement_cutover.py` |
| Writes | entity, business unit, categories, encrypted evidence, PENDING candidates | statement, transaction facts, evidence |
| Role | owner | owner |
| Idempotence | receipt in `internal_import.controlled_batch_receipt`, keyed by `batch_ref` | replay comparison inside one transaction |
| Precondition | an operator-written source manifest | an encrypted backup **and** a passed isolated-restore rehearsal |
| Runs locally | yes | yes — see [Statements](#statements) |

Both refuse to post anything. Candidates arrive `PENDING`; `Ledger Draft ->
Posted Entry` still needs a person, locally as everywhere else.

## The path that runs locally

```text
python scripts/local_mode.py init
docker compose --env-file .env.local -f docker-compose.local.yml up -d
python scripts/local_mode.py migrate
python scripts/local_mode.py import --source-manifest <absolute path>
python scripts/local_mode.py serve
```

`import` does two things production runs as separate commands
(`prepare_controlled_review_import.py`, then `import_controlled_review_batch.py`),
joined here because locally they are one intention:

1. Encrypt every evidence file named by the manifest into the artifact store
   under `~/.ledgerbridge-local/artifacts`, using the key at
   `~/.ledgerbridge-local/keys/evidence-key.json`. The key is created on first
   use and never rotated; it lives outside the working tree so that nothing
   under the repository holds a key by accident.
2. Write the batch inside one transaction as the owner role, then append its
   receipt.

The database URL is read out of `.env.local`, so importing never involves
pasting a password into a shell. Nothing about the batch is printed except its
counts.

### The source manifest

One JSON file, schema `ledgerbridge.controlled-review-source.v1`, validated
strictly — unknown fields are rejected. The tracked synthetic example is the
fixture in `tests/test_controlled_import.py`; keep real manifests outside Git.
Required: `batch_ref`, `generated_at`, `source_description`, one `entity`, one
`business_unit`, at least one `category`, at least one `evidence` descriptor
(with the plaintext SHA-256 and byte size of the file, which are verified
before encryption), and at least one `candidate`.

Two things bite on the first attempt:

- **`candidate_ref` prefixes must differ across batches.** The candidate's
  `short_id` is derived from the first eight hex characters of its UUID, and it
  is unique per entity. Two fixtures numbered `7000...01` and `7000...02` both
  become `C-70000000` and the second import fails on the unique constraint.
- **A second prepared manifest for the same `batch_ref` conflicts.** Preparing
  again publishes a new encrypted object, so the prepared digest changes, and
  the import refuses with *batch receipt conflicts with prepared manifest*
  rather than writing twice. To re-run a batch, point `--prepared-manifest` at
  the one already prepared.

### Replaying

Running `import` again on the same batch re-reads its own receipt and writes
nothing:

```text
LOCAL_IMPORT_OK replayed=false evidence=1 candidates=1 batch=<ref> horizon=43
LOCAL_IMPORT_OK replayed=true  evidence=1 candidates=1 batch=<ref> horizon=43
```

The unchanged `horizon` is the check worth making: it is the audit-chain
sequence at the time of the receipt, so an unchanged horizon means no event was
appended. This is how to resolve a run that died halfway, rather than by
counting rows.

## Roles, per step

Local mode keeps production's role separation rather than logging in once as
something roomy. The live database has already caught this twice.

| Step | Role | Why not a wider one |
|---|---|---|
| `migrate` | `ledgerbridge_owner` | migrations create objects |
| `import` | `ledgerbridge_owner` | the batch writes dimensions, evidence and candidates |
| listing the books at startup | `ledgerbridge_owner` | the reader cannot select from `entity`; asked once, on a separate connection, before the first request |
| every served request | `ledgerbridge_reader` | it can execute the `internal_read` definer functions and select from no base table at all — the boundary the whole read design rests on |
| appending "this document was opened" | `ledgerbridge_api` | the reader cannot write, and the connection that answers a question should not also write the record of having answered |

An earlier version served requests as `ledgerbridge_api`. Every unit test
passed; the live database refused with *permission denied for table entity*.
That was the design working.

## The local database really is Core's, and how that was checked

The repository carries PostgreSQL integration tests that are skipped unless
given real role URLs. They create their own temporary databases and assert the
privilege boundaries the read design rests on: the runtime login stays
unprivileged, it cannot create temporary tables, the audit function ACL is
append-only, the canonical source registries are read-only to the runtime,
permanent evidence cannot be mutated.

Run against the local container on 2026-09-12, the eight files holding those
tests passed in full — 178 tests, including all five privilege assertions.
That is what distinguishes *a PostgreSQL that holds the rows* from *Core's
database*.

**Do not run the whole suite against the local database.** Two things happen,
both discovered the hard way:

- Something in the wider suite empties `ledgerbridge` when
  `LEDGERBRIDGE_DATABASE_URL` names it. The schema survives at head; every row
  does not. Re-import afterwards.
- Exporting the `LEDGERBRIDGE_*` URLs process-wide also fails 117 tests that
  pass with a clean environment, because they construct `Settings()` expecting
  one.

A `local_mode.py verify` subcommand that did exactly this was written and then
removed for those two reasons. If the check is wanted again, it needs to name
the integration files explicitly and point them at a throwaway database.

Getting the roles right matters here too: handing every variable the owner URL
turns the five privilege assertions into failures that look like database
defects and are actually a mis-wired runner.

## Statements

This is the path that writes bank statement transaction facts, and the previous
version of this document said it does not run here. That was wrong, and it is
worth saying why, because the reasoning that produced it is the kind that
quietly turns a gate into a wall.

`verify_mybank_cutover_safety_proof` runs before the preflight and the execution
alike, and demands:

- a backup directory holding `backup.json` (format
  `ledgerbridge-encrypted-backup-v3`, with a GPG fingerprint and the
  ciphertext's SHA-256), `ledgerbridge-backup.tar.gpg`, and a matching
  `SHA256SUMS`;
- a `restore-rehearsal-*.json` beside it, format
  `ledgerbridge-restore-rehearsal-v3`, `status: passed`,
  `production_unchanged: true`, `isolated_resources_removed: true`;
- and that report's `cutover_inventory` to equal the live database's counts
  exactly.

Those artifacts used to come only from `scripts/backup_restore.py`, which is a
Hermes script — `/srv/ai-center`, `/dev/shm`, GNU `tar` — and does not run on
Windows. The conclusion drawn from that was "statements cannot be imported
locally". But the gate validates *artifacts*, not their producer. It asks for
proof that a restorable backup exists and was rehearsed; it does not ask which
machine did the rehearsing.

`src/ledgerbridge/local_backup.py` does that work here, and does it honestly:

1. `pg_dump -Fc` of the live local database, through `docker exec`.
2. A deterministic tar of the dump, the inventory, and the artifact store — no
   timestamps, no ownership — so that an unchanged database backs up to
   identical bytes and a changed ciphertext digest means a changed ledger.
3. Real GPG encryption to a key in a keyring of its own under
   `~/.ledgerbridge-local/gnupg`, never the user's.
4. A real restore: decrypt, `createdb` a throwaway database, `pg_restore` into
   it, read its inventory, compare it to the source's, then drop it and prove
   both that it is gone and that the live database did not move.

The inventory counts every base table in `public`, not only the sixteen the gate
reads by name. Extra keys are ignored by `_counts_from_cutover_inventory`, so
counting everything is stronger evidence that the restored database equals the
live one, not weaker.

Nothing was weakened and nothing was hand-written. `local_mode.py statements`
takes the backup itself, immediately before importing, because the inventory
must match the database as it is at import time — any write in between (another
account admitted, another batch imported) invalidates it, and the failure would
otherwise arrive as an opaque "proof is invalid".

```text
python scripts/local_mode.py book       --plan <account>.intake.json
python scripts/local_mode.py statements --manifest <book>.statements.json
```

### The batch manifest

`ledgerbridge.local-statement-batch.v1`, keys exactly
`{schema_version, book, accounts, statements, audit}`. `accounts` maps each
account suffix to the Managed Account ref registered for it; every statement
names a suffix that must appear there. At most 100 statements, because that is
the cutover's rollback boundary; a longer manifest is refused here with a
message that says to split it rather than failing deep inside the cutover.

`expected_new_transaction_count` declares how many of a file's rows the ledger
has not seen. Omitting it asserts that all of them are new, which is right for a
first import and wrong for a re-export that overlaps one.

### Accounts

`account_registry_intake.run_transactional_account_registry_intake` has no
environment gate — only the command wrappers
(`account_registry_intake_command.py`, `scripts/run_account_registry_intake.py`)
demand `LEDGERBRIDGE_ENV=production`, a `DEPLOYED_REVISION` file and an
execution token. The module itself creates entity, business unit, admission
evidence, managed account, aliases and the business-unit assignment in one
transaction, then replays itself and requires an exact replay. `local_mode.py
book` uses it directly.

Two things about it are easy to get wrong:

- **A managed account needs continuous business-unit assignment coverage over
  the statement period.** Without it the import fails as
  "existing-account statement import conflict", which names the symptom and not
  the cause. The intake plan therefore carries an assignment with an
  `effective_from` that precedes every statement.
- **`managed_account_alias` is unique on `(institution_code, alias_kind,
  normalized_value)` across the whole database**, not per entity. A suffix alias
  belongs to exactly one account anywhere. Claiming one for a test entity blocks
  the real account that owns it, permanently: every registry table is
  append-only by trigger, so there is no un-claiming it.

#### An account with no account number

`account_suffix` must be four to eight digits, and a payment wallet has none.
零钱 and 零钱通 are admitted with `0001` and `0002`, which are an ordinal of
ours and not a masked account number — so they carry no `SUFFIX` alias, only a
`LABEL` alias holding the wallet's real name. That matters because a suffix
alias is a claim, unique across the database, that some account ends in those
digits; making it up would block whichever real account one day does. The
account is identified by its `account_key` instead.

The admission evidence can be a document that merely *names* the account. For
these two it is the WeChat export, the same file for both, whose 支付方式
column names them on every row they fund.

### When two exports of the same account disagree

MYbank exports one account in more than one column layout. The daily statement
carries eleven columns; some range exports carry nine, dropping 交易名称 or
对方账号. The parser renders a missing name column as the literal
`银行未提供交易名称`, so the same transaction read from two files is the same
amount, date, balance and serial with a different name.

The ledger keys a fact on `(managed_account_ref, transaction_serial)` and
refuses a second, disagreeing version of it — correctly, because silently
overwriting a fact is how a ledger stops being one. The database raises
`overlapping bank statement transaction conflicts with fact`, which the cutover
reports as `existing-account statement import conflict`.

There is no enrichment path: a fact cannot be improved by a later, fuller
statement. So one export has to be the book's. Keep the file that covers the
most transactions, and admit a further file only when it either agrees with what
is kept or brings serials nobody has seen. A file that both disagrees and adds
nothing is the one to leave out — and the trade-off is worth naming rather than
automating, because it is a judgement about which of the bank's own documents
the book is based on.

### And when they agree

The same month downloaded three times is the other half of this. Three
byte-different `月账单-202608` files for one MYbank account carried the same six
facts over the same period, so nothing disagreed — and the batch was still
refused, with `statement batch contains duplicate identities`. The batch gate
requires `(managed_account_ref, period_start, period_end)` to be unique within a
batch, and it is right to: two statements claiming one account and one period is
not something a ledger should hold, however well they agree.

So the rule for leaving a file out is not "it disagrees and adds nothing" but
simply "it brings no serial the book does not already hold". A duplicate
download and a thinner re-export are the same thing to the ledger, and neither
is worth a second statement row.

## Payment platforms: candidates, not statements

A bank statement and a WeChat bill are different kinds of document, and the two
import paths are not interchangeable.

A statement is an account's own record. It carries a running balance, and every
parser here uses it as a continuity proof - previous balance plus amount equals
this balance - which is how a missing row is caught. A payment bill records
what was paid to whom. WeChat's carries no balance, and no single account for
one to belong to: one export draws on 零钱, 零钱通 and several bank cards.

WeChat does keep 零钱 and 零钱通 as proper accounts with per-row balances; they
are visible in the app and cannot be exported. The bill is missing exactly the
rows that would chain them - the sweeps from 零钱 into 零钱通, and the daily
零钱通 interest. So the balance cannot be recovered from this document, and
computing one from the bill's own rows and then checking it against them would
be a check that always passes.

Instead the rows enter as **candidates**: real transactions that nobody has
classified. That path needs no balance, and needs no backup and no restore
rehearsal either, because nothing it writes is a fact a person has confirmed.
It already carries the receipted idempotent replay D-028 asks for.

```bash
uv run python -X utf8 scripts/local_mode.py candidates --manifest <batch.json>
```

The manifest names the book and the export files, nothing else. What the
importer checks, and what it refuses to decide:

- **The file's own completeness proof.** The preamble states the record count,
  and the count and total of income, expenditure and direction-less rows. An
  export whose rows disagree with any of the four is refused. This is what
  stands in for the balance chain.
- **Overlapping exports.** The same transaction appears in several exports.
  The money must agree - time, direction, amount, type, funding, merchant
  reference - and a disagreement stops the batch. The description may differ:
  a counterparty's display name is their profile, and a refunded payment shows
  a later status, so the newer export wins on those.
- **Direction.** WeChat gives some rows no direction at all - top-ups,
  withdrawals, 零钱通 movements, card repayments - and totals them separately,
  unsigned. They keep the magnitude and land in their own category. Reading a
  sign out of the transaction type would be inference dressed as a fact.
- **Classification.** None. Every candidate arrives at zero confidence, which
  is the honest statement of what an importer knows.

### Two platforms, and which one is named

WeChat and Alipay ship the same kind of document. `local_payments.py` holds
what a payment bill is — the row, the export, the three directions and the
preamble check — and each reader produces it. The batch manifest carries a
`platform` key naming which reader to use; the loader does not sniff. Guessing
a financial document's format wrong is worse than being told.

Two things differ, and both are the file's own doing rather than ours:

- **Alipay's amount totals do not bind.** Its footnote 6 says summing the
  detail amounts may not match the totals it prints, and its files bear that
  out. The counts bind — all four, on every file — because a row that went
  missing changes one. WeChat states no such caveat and its totals reconcile
  exactly, so they stay enforced.
- **Alipay names the account, WeChat does not.** A batch may not mix two
  accounts, and the builder refuses files whose preambles name different ones.
  Import each login as its own batch.

### A payment's identity is the account plus the order number

Not the order number alone. Two Alipay logins transferring to each other write
one order number into both bills — once as money going out, once as money
coming in — and those are two facts, one per account. The derived references
therefore carry the account, folded in as a digest: a login is an email address
or a phone number, and a reference is stored forever.

WeChat's bill names no account, so nothing is added to its references, and they
are the ones its 2,212 candidates are already in the ledger under.

### The manifest must not read the clock

The batch receipt tells a replay from a second import by comparing manifest
digests. A `generated_at` taken from the clock makes every re-run a different
batch, and the receipt then refuses the replay it was supposed to recognise.
The builder derives that timestamp from the newest export instead, which makes
the source manifest byte-identical run to run.

That is not enough for a replay, and it is worth knowing why. Preparing the
manifest encrypts the evidence, and the envelope is not reproducible, so the
*prepared* digest differs every run:

```text
source manifest    d2e1fe1a94879641 d2e1fe1a94879641 same
prepared manifest  1ab9a6d76a6b0b6d 7d7ef31aed77a375 DIFFERENT
```

So re-running any candidate manifest is refused with "batch receipt conflicts
with prepared manifest". Fail-closed and safe — nothing double-imports — but it
is a refusal, not the idempotent replay D-028 describes. Closing it means
changing Core's receipt or its envelope, and neither is done here.

## Views that stay dark locally

Four GET routes are mounted but answer `404 CANDIDATE_COMMAND_DISABLED`:
`/internal/v1/candidate-events`,
`/internal/v1/candidate-classification-groups`,
`/internal/v1/company-transaction-classifications` and its summary. They live
inside command routers, and Core gates the whole module behind
`enable_internal_candidate_command_api`, which also needs a command assertion
key, issuer and audience.

Their read routes are mounted anyway, without the commands beside them, so the
failure says *disabled* rather than *not found* and turning the module on later
is a settings change rather than a re-assembly. Whether to turn it on is the
user's call: it means minting an assertion key for a local machine, and saying
"the command API is enabled" in a profile that deliberately serves no command.

## Open question for the user

D-028 says that once an import capability has been released and restore-
verified, importing further files uses a bounded preview, a transactional
write, a receipt, a count check and an idempotent replay — and explicitly does
not repeat the whole recovery rehearsal for each file.

The code has not implemented that relaxation for statement cutovers. Every
statement import still verifies a full encrypted-backup-and-restore proof, and
`docs/operations/MYBANK_CUTOVER.md` still instructs one fresh backup/restore
inventory per plan. The controlled-review batch path above is the shape D-028
describes, but it writes candidates rather than statement transaction facts.

Closing that gap would mean changing a gate on the path that writes real
financial facts. That is not a change to make in passing, so it is recorded
here and in the task document, and left for the user to decide.
