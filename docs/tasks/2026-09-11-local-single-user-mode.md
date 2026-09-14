# Task: Local single-user mode

- Status: active
- Implementation owner: Claude (`ai/claude/local-single-user-mode`)
- Review owner: user
- Branch: `ai/claude/local-single-user-mode`, worktree
  `D:\repos\_worktrees\ledgerbridge-local-mode-20260911`, based on `448d700`
- Owned files: `docs/tasks/2026-09-11-local-single-user-mode.md`,
  `docs/operations/LOCAL_IMPORT.md` (new), `scripts/local_mode.py` (new),
  `docker-compose.local.yml` (new), `.env.local.example` (new),
  `src/ledgerbridge/local_mode.py` (new), `src/ledgerbridge/local_backup.py`
  (new), `src/ledgerbridge/local_statements.py` (new), and their tests.
  No Alembic migration, no change to `config.py` defaults, no change to any
  existing route, service, or production manifest. One existing module is
  touched — `src/ledgerbridge/account_registry_intake.py`, a schema-revision pin
  — and it is open issue 2 below, not a merged decision.

## Why this exists

The user wants the ledger usable on one machine without the production
deployment. A previous attempt built that as a separate program in a separate
repository (`D:\repos\finance-desk`, 30 commits, no remote, never recorded in
`work.md` or any handoff). That violates D-015 and D-028: one product, one
authoritative fact layer, no second set of real business facts. It also
duplicated work that already exists here — production already holds 11
statements and 2,447 transaction facts from the same company sources.

This task replaces that attempt. Nothing new is invented about what a financial
fact *is*; the local mode only changes how the existing Core is started and who
is allowed to talk to it.

## Goal

One command starts Core against a local PostgreSQL on loopback, and a local
read-only UI shows the same accounts, statements, facts and reports that the
deployed Core would show — with no mTLS proxy, no Passkey, no Hermes, and no
cloud host.

## Key finding: this is a profile, not an architecture change

`Settings` already carries every switch this needs, and they are already
default-off:

| Setting | Production | Local mode |
|---|---|---|
| `internal_read_backend` | `database` | `database` (same) |
| `internal_read_transport` | `unix-mtls-proxy` | `disabled` |
| `internal_read_operational_gate` | `r1-production-v1` | `closed` |
| `enable_internal_read_api` | `true` | `true` |
| `env` | `production` | `development` |

`scripts/r1_synthetic_demo.py` already establishes the shape of a loopback-only
launcher with a fixed local principal. Local mode is that launcher pointed at a
real local database instead of the packaged fixture. No new fact model, no new
table, no new writer.

## In scope

- `docker-compose.local.yml`: postgres + migrate only, bound to `127.0.0.1`,
  a local volume, no published port beyond loopback.
- `scripts/local_mode.py`: start Core with the local profile, refusing to run
  if any production-only setting is present (see Frozen invariants).
- A local read-only UI over the existing `/internal/v1` GET routes.
- Importing the user's statements through the **existing** importers
  (`bank_statement_*`, `mybank_*`, `abc_statement`, `boc_statement`), with the
  same preflight → transaction → receipt → idempotent replay discipline.
- Carrying over from `finance-desk` only what is genuinely missing here, as
  separate reviewed changes: the BOC company CSV and PDF container readers, and
  the classification rule set. Both are parser/rule level, not fact level.

## Out of scope

- Any weakening of the production path. Local mode must not make it easier to
  run production without mTLS, the reader role, or the operational gate.
- Automatic posting. Local mode is read-only over facts; classification stays a
  rule proposal, and `Ledger Draft -> Posted Entry` still needs a human.
- Replacing `~/.finance-desk` data by copying its SQLite rows in. Those rows
  were produced by a parser outside this repository's evidence discipline; the
  statements get re-imported from the original files instead.
- OAuth, mail collection, Hermes, OneDrive, any network connector.

## Frozen invariants

- One authoritative fact layer. Local mode reads the same schema through the
  same code; it does not define a second one.
- Money stays signed integer minor units; entries balance per currency in
  PostgreSQL.
- No LLM, rule, or importer writes a posting.
- Local mode refuses to start when `env=production`, when any
  `*_operational_gate` is open, or when `internal_read_transport` is not
  `disabled` — the failure is loud, not a silent downgrade.
- Real statements and artifacts are never committed.

## Acceptance tests

- Starting local mode with a production-shaped environment fails closed, with a
  test that asserts the refusal rather than the success path.
- The local launcher binds loopback only; a test asserts the bound host.
- The same query answered by the deployed read API and by local mode over a
  restored dump returns the same facts.
- Importing one statement twice in local mode produces an exact zero delta.

## Decisions (2026-09-11, user)

1. Local storage is PostgreSQL in Docker on loopback. Core's invariants
   (balanced entries, `internal_read` definer functions, triggers) are enforced
   *in PostgreSQL*; a file-backed store would lose them and recreate the
   second-fact-layer problem somewhere new.
2. The UI is `LedgerBridge-Web` pointed at loopback, not a new desktop window.
   That needs a fourth Web mode beside `synthetic-preview`,
   `authenticated-preview` and `core-backed`: plain HTTP to Core over loopback,
   no client certificates, no Passkey, and a refusal to start on a non-loopback
   bind.
3. `finance-desk` and `~/.finance-desk` are frozen in place, not deleted. They
   stay readable for comparison until local mode answers the same questions;
   nothing new is written there.

## Implementation evidence

- `src/ledgerbridge/local_mode.py` and `tests/test_local_mode.py`: the profile
  and its refusals. 11 tests, with `ruff`, `ruff format` and `mypy` clean.
- Two intended refusals turned out to be enforced by `Settings` itself (a
  production read API without the R1 gate, and real ingest). Those tests assert
  the upstream `ValidationError` and keep local mode's own check as a second
  net, exercised through `model_copy` so that relaxing the upstream validator
  turns this suite red instead of silently emptying the net.
- Still to do: `docker-compose.local.yml`, `scripts/local_mode.py`, the Web
  local mode, importing the statements, and the parser and rule carry-over.

## Baseline correction (2026-09-11)

This task started from `448d700` on `ai/chatgpt/company-mybank-production-import`,
named as the integration branch by `PROJECT_STATUS.md`. That document is dated
2026-09-02 and is stale: `448d700` sits two commits past its merge-base with
`production/core`, which is 121 commits ahead. The branch has been rebased onto
`production/core`; the local database followed from schema `20260902_0034` to
`20260906_0051`, which is also the first real application of that last
migration - it had been verified by source assertion only.

## Later decisions (2026-09-11, user)

4. The review screen keeps its write buttons in local mode. Core's local mode
   serves only the GET read routes, so a confirm or ignore POST fails at Core.
   The UI is not yet honest about that; it was left alone rather than redesigned
   in passing.
5. The BOC company CSV and PDF readers stay on a local branch. They are merged
   into this branch for local use and do not enter the production release line.

## Local review opened (2026-09-13, user: "开审核", then "去掉签名")

This changes a frozen invariant, with the user's approval: local mode was
"read-only over facts", and it now **writes candidate decisions**. Decision 4
above is superseded - the write buttons are no longer dead ends. Nothing else
about the invariants moves: no importer, rule or model posts; no posting, no
payment, no evidence unlock, no candidate supersede, no registry change, no
bank-statement review and no company classification review is served.

It was first built with the production user assertion: the BFF signed each
decision with a key Core minted per start and left in a per-run file. The user
then asked for the signature to go ("去掉签名"): both processes run as the same
person on the same machine, so it proved nothing already true, and it cost a
key file, a start order and a failure mode. What stands now:

- **Two routes, from one module.** `ledgerbridge.local_commands` serves
  `POST /internal/v1/candidates/{candidate_ref}/decisions` and
  `POST /internal/v1/candidate-classification-groups/{group_ref}/decisions`
  with no `X-LedgerBridge-User-Assertion`. The production routes in
  `internal_candidate_command_routes` are untouched and still verify it; local
  mode mounts only their GET routes. `_refuse_unlisted_writes` fails the start
  on any other writing route, and on these two paths served by any module but
  `local_commands` - otherwise the signed and unsigned copies could both be
  mounted and whichever came first would win.
- **Below the envelope, nothing changed.** Each decision goes through the same
  service and the same `internal_command` database functions under the api
  role: capability, entity and business-unit scope, revision check, idempotency
  receipt, append-only audit chain. The database wants an assertion id; locally
  it is `uuid5(namespace, operation_id)`, so a retry of the same operation
  replays. The actor is `local-single-user`, the BFF session's principal.
- **The unsigned router refuses a deployed configuration.**
  `require_local_profile` answers 404 unless env is not production, the read
  transport is disabled and both operational gates are closed, and a test
  asserts `ledgerbridge.main.app` never mounts it.
- **One capability.** The local identity is exactly
  `READ_CAPABILITIES | {candidate:decide}`.
- **DNS rebinding is refused on both sides.** Without a signature, the loopback
  bind alone would let a website whose own name re-resolves to 127.0.0.1 read
  candidates and decide them - through the BFF (same-origin, so it could read
  the CSRF token) or straight at Core. Core's `LoopbackOnlyMiddleware` answers
  `421 LOCAL_HOST_REJECTED` to any Host but `127.0.0.1:8661`/`localhost:8661`
  and to any non-GET carrying an `Origin`; the BFF refuses any Host but this
  machine's names on its own port. Found by the review below.
- **Kept on the Web side:** the local session cookie and its CSRF token.

Proved live on the rebuilt books, writing nothing (2026-09-13, after the
change): through the BFF a CONFIRM without the CSRF token got `403`; with it,
unsigned and with a deliberately stale revision, `409 STALE_REVISION` from the
database function. A rebound Host got `421` from Core (GET and POST) and from
the BFF; a POST to Core carrying an `Origin` got `421`; `localhost` got `200`.
`candidate-events` for the probed candidate stayed at 0 throughout. The first
real decision is the user's to make.

### Review of the whole flow (2026-09-13, two read-only reviewers)

Fixed in this change: the rebinding hole (above), the router's missing guard
against production, the path-only startup check, the capability test that
listed exclusions instead of asserting the set, stale comments.

Still open:

- ~~**A confirmed 不计收支 row counts as income.**~~ Fixed 2026-09-13 (user:
  "全部修复"): `personal_finance_summary._cashflow_minor` now books a row the
  platform calls 不计收支 as no cash flow, and such a row is counted as neither
  an income nor an expense entry. This is a production module; it changes no
  production total, because a production neutral row would have been
  mis-summed the same way.
- **What counts after CONFIRM.** A confirmed platform candidate enters the
  personal summary as it is; no category correction is needed. A book whose
  unit name says 公司/酒店/宾馆/门店 is excluded. Category shares read
  微信交易复核/支付宝交易复核 until corrected, and a correction can only pick a
  category that already exists for the entity.
- ~~**Web UI, local mode.**~~ Fixed 2026-09-13 (Web branch, "全部修复"): the
  Passkey and logout menu items, the evidence unlock dialog and the payroll
  entry are hidden locally; a 409 on a single decision re-reads the candidate;
  a decision retried after a network error or 502/503/504 reuses its
  idempotency key and replays; BFF problem codes show Chinese messages; the
  local session renews on use and on reopening the page; a path reference
  must be a canonical UUID. HTTP-level BFF decision tests were added.
- **Category corrections can only pick an existing category.** Local books
  hold only the two platform review categories. Real categories arrive with the
  finance-desk rules carry-over, the next step.
- **Not yet written:** group/IGNORE/CORRECT through the local Core app, and a
  database-backed replay test for the derived assertion id.

A correction: the test added on 2026-09-12 to show enabling the module
"mounts no command" walked `app.routes`, where FastAPI keeps included routers
wrapped - it found no routes at all and passed by seeing nothing. The startup
check itself was never affected (it walks the routers); the replacement test
walks them too.

## Import chain (2026-09-12)

`docs/operations/LOCAL_IMPORT.md` is the runbook. Summary of what it settles:

- The controlled-review batch path (`controlled_import.py`) runs locally,
  unmodified, and is the shape D-028 describes: bounded preparation,
  transactional write as the owner role, receipt, idempotent replay. It is now
  reachable as `local_mode.py import`, which reads the database URL out of
  `.env.local` so importing never means pasting a password into a shell.
- The registered-account statement cutover path runs here too, which corrects
  what this section said first. `verify_mybank_cutover_safety_proof` validates
  *artifacts* — a v3 encrypted backup and a passed isolated restore rehearsal
  whose inventory equals the live counts — not the script that produced them.
  `local_backup.py` produces them on this machine by doing the work: a real
  `pg_dump`, a deterministic archive, real GPG encryption to its own keyring, a
  real restore into a throwaway database, a real inventory comparison, and proof
  that the throwaway is gone and the live database did not move.
- `run_account_registry_intake.py` does refuse unless
  `LEDGERBRIDGE_ENV=production`, but that gate is in the command wrapper. The
  module's `run_transactional_account_registry_intake` has none, and is what
  `local_mode.py book` calls.

### Proved against the live local database, 2026-09-12

- Import, then replay: `replayed=false` then `replayed=true`, with an unchanged
  audit horizon and a zero row delta across `entity`, `candidate`,
  `evidence_object` and `audit_event`. This is the task's fourth acceptance
  test, met for the batch path.
- A second batch appeared in `check` without anyone widening a list
  (`entities: 2`, `business_units: 2`).
- `serve` on `127.0.0.1:8661` (confirmed loopback-only by `netstat`) answered
  `/internal/v1/capabilities`, `/internal/v1/candidates` (2 rows, from both
  books) and `/internal/v1/personal-finance-summary`, each `Cache-Control:
  no-store`.
- `/internal/v1/evidence/{ref}/content` returned the decrypted synthetic
  document, and the read appended `internal.read.evidence.content` by
  `workload:local-single-user` to the audit chain.

## Findings the live data forced (2026-09-12)

Three defects that every unit test passed through, all found by importing real
rows rather than by reading the code:

1. **The local grant saw nothing that was assigned.** `local_principal` granted
   the entity with an empty business-unit set. The read design keeps two
   independent keys per unit - the ref the HTTP layer authorizes on and the UUID
   the scoped SQL functions query - and refuses to resolve one into the other,
   so an assigned candidate was invisible while the page rendered cleanly. Fixed
   by carrying `LocalBook(entity_ref, business_units)` through the bootstrap.
2. **The local app mounted one router.** `/capabilities` answered, so the
   assembly test passed; the Web client also asks for statements,
   reconciliations, reports and personal finance, all of which would have 404ed
   and read as missing data. Now the six GET-only routers `main.py` mounts are
   mounted, and `_refuse_non_read_routes` makes "local mode serves reads only" a
   property the process asserts about itself before it binds a socket, rather
   than a list someone has to keep curating.
3. **Evidence downloads failed closed.** They need an evidence key file and a
   persistent audit sink, and the sink writes through the api role - which local
   mode had collapsed into the reader. The profile now carries both URLs and the
   guard checks both hosts.

## Statements imported (2026-09-12)

The statement cutover does run here. `src/ledgerbridge/local_backup.py` produces
the encrypted backup and isolated restore rehearsal the gate demands, on this
machine, without weakening the gate or hand-writing an artifact;
`src/ledgerbridge/local_statements.py` imports a whole book under one rollback
boundary. `docs/operations/LOCAL_IMPORT.md` has the reasoning and the
corrections it forced.

All eight books admitted and imported from the user's real files, into the
database rebuilt on 2026-09-12 (see "The rebuild" below):

| Book | Account | Statements | Facts | finance-desk's own count |
|---|---|---|---|---|
| 星汇 | mybank-0688 | 1 | 74 | 74 |
| 景怡 | abc-9018 | 9 | 106 | 106 |
| 薇旭 | boc-6492 + mybank-3678 | 36 | 842 | 842 |
| 逸豪 | mybank-2083 | 2 | 652 | 652 |
| 雅朵 | abc-3234 | 5 | 228 | 228 |
| 雅阁 | mybank-9191 | 3 | 85 | 85 |
| 青居客 | mybank-2825 | 1 | 89 | 89 |
| personal | ccb-7564, abc-2061, abc-7177, mybank-7968 | 7 | 645 | 645 (of 9,592) |

64 statements, 2,721 facts, 14 accounts, 8 books. Every company count matches
finance-desk's row count exactly, computed independently: finance-desk's is its
own parser's output, this one is Core's. All reviews are `PENDING`; nothing is
posted.

Two of those fourteen accounts hold no statement: 零钱 and 零钱通, admitted as
wallets (see "WeChat's two wallets" below). The other twelve are the eleven
above plus ABC personal `…7177`, which the merchant-reference decision unlocked.

Beside the statements sit **8,947 review candidates**, all PENDING: 2,212 from
WeChat and 6,735 from Alipay's two logins. They are payments, not statements,
and they are counted separately for that reason.

## The rebuild (2026-09-12, user approved: "重建库")

Open issue 1 below was closed by rebuilding, and nothing was destroyed. The old
database was renamed rather than dropped — `ledgerbridge` →
`ledgerbridge_pre_rebuild_20260912`, 22 MB, still present — and the artifact
root moved aside to `~/.ledgerbridge-local/artifacts-pre-rebuild-20260912` with
the evidence key left in place. A fresh `ledgerbridge` was created and migrated
to `20260906_0051`: 59 tables, 0 entities, 0 accounts, 0 facts. The rename is
reversible.

Every book was then re-admitted and re-imported from the manifests. 星汇 was
admitted for the first time, which is what the rebuild was for.

### One MYbank account, the same month downloaded three times

星汇's batch was refused with `statement batch contains duplicate identities`.
Three byte-different `月账单-202608` files carried six rows each, the same six
facts, over the same period; the batch gate requires
`(managed_account_ref, period_start, period_end)` to be unique within a batch.
This is not a fact disagreement — it is the same statement downloaded three
times — and the gate is right to refuse it: two statements claiming one account
and one period is not something a ledger should hold, however well they agree.

The manifest generator now drops any file that brings no serial the book does
not already hold, whether it disagrees or agrees. For 星汇 that left one
74-row range export carrying all 74 facts; the six dropped files contributed
nothing. Zero facts were lost.

## Second rebuild: candidates in Core's shape (2026-09-13, user: "重建本地 ledgerbridge 库和 artifacts 目录")

The local importer had written WeChat and Alipay candidates in a shape of its
own - a slash-joined summary without platform, date or direction, categories
`UNCLASSIFIED`/`INTERNAL_TRANSFER`, Alipay as `alipay_bill_export`. Core reads
the summary positionally (`personal_finance_summary`) and raises platform
review risks only for `WECHAT_/ALIPAY_TRANSACTION_REVIEW` (`review_risk`), so
every local candidate would have dropped out of personal totals once confirmed,
and none carried a risk. Nothing had been reviewed yet, so it was fixed at the
source (`2836bfe`, matching `scripts/build_platform_review_bundle.py`) and the
database rebuilt.

Nothing was destroyed. Backup `local-backup-20260913T094507Z` was taken first;
the old database is `ledgerbridge_pre_contract_20260913` and its artifacts
`~/.ledgerbridge-local/artifacts-pre-contract-20260913`. All 14 accounts, 9
statement batches and 3 candidate batches re-imported without a failure.

```text
                         statements  txn   obs   accounts entities candidates evidence
pre_contract_20260913    64          2721  3549  14       8        8947       72
ledgerbridge (rebuilt)   64          2721  3549  14       8        8947       72
```

Through the running Core, all 8,947 candidates have the seven-field summary
(2,212 `wechat_pay_export`, 6,735 `alipay_export`), in 微信交易复核 / 支付宝交易复核,
and review risks now appear where they belong:

```text
TRANSFER_REVIEW_REQUIRED    6003
FUNDING_STATEMENT_REQUIRED   644
REVERSAL_MATCH_REQUIRED      300
UNSETTLED_TRANSACTION        103
no risk                     2142
```

`personal-finance-summary` reports 8,947 pending and 8,947 excluded, which is
correct before any decision: only confirmed candidates become entries. That the
shape is accepted once confirmed is held by the unit tests, not yet by a live
confirmation - local mode serves no decision command.

The rebuild also retires the WeChat batch's replay wart recorded under open
issue 4: that batch was first imported from a manifest carrying a clock
reading, and it has now been imported from the reproducible one. The general
candidate-replay issue (open issue 8, nondeterministic evidence envelope) is
unchanged.

## Open issues (awaiting user decision)

1. ~~**星汇 (book 01) cannot be admitted, because of a mistake of mine.**~~
   **Closed 2026-09-12 by the user: "重建库".** An earlier end-to-end proof of
   mine had registered the suffix alias `mybank`/`SUFFIX`/`0688` under a
   synthetic entity, and `managed_account_alias` is unique on
   `(institution_code, alias_kind, normalized_value)` across the whole
   database; every registry table is append-only by trigger, so the claim could
   not be withdrawn in place. The database was rebuilt — by rename, not by drop
   — and 星汇's 74 facts are now in the ledger. See "The rebuild" above.

2. ~~**`src/ledgerbridge/account_registry_intake.py` was changed, and this is
   the approval request AGENTS.md asks for.**~~ **Approved 2026-09-12 by the
   user: "批准修改".** Its schema pin was a single revision, `20260904_0044`,
   so against head (`20260906_0051`) the intake failed as "account intake
   database owner target is invalid" — the same message a wrong role produces.
   The pin is now a frozenset through `20260906_0051`, with a separate message
   naming the real cause. Migrations 0045–0051 were read first: counterparty
   overlap, candidate readers, reporting items, a payroll read model,
   reporting-item correction, a POSTED snapshot trigger fix and two parser
   profiles. None touch registry tables. `mybank_statement_cutover.py` already
   keeps its supported revisions as a frozenset through the same revision, so
   this aligns siblings rather than inventing a practice. It stays on this
   local branch (D-005, and the user's "留着。本地。"), not on the release line.

3. **Two parser gaps, both in what Core admits as an official statement.**
   The user's "优化解析器" authorised fixing these.
   - ~~7 MYbank monthly files rejected as "company range statement header is
     invalid".~~ **Fixed (`3189458`).** Their header uses half-width
     `借方金额(收)` where `_COMPANY_RANGE_HEADERS` required full-width `（收）`;
     the bank ships both spellings of one label. Header lookup now folds the
     two parenthesis code points together, so one layout needs one entry rather
     than a second copy of every tuple. Nothing else about a header is
     normalised. All 7 files parse; two of them are in the ledger (雅阁 and
     薇旭 each gained a statement), the rest brought no facts the books did not
     already hold.
   - ~~2 ABC personal PDFs rejected as "statement transaction log number is
     invalid".~~ **One fixed (`14c6801`), one is a decision.**

     The first: ABC prints the counterparty account in a fixed-width column,
     and a 17-digit account runs into the 16-wide slot, carrying the log
     number's leading digit with it. The digit is now handed back, but only
     when the log column is over-long, the stolen part is digits continuing
     the account, what remains is a whole log number, and the two fields are
     printed with no gap. A row whose log column already holds a well-formed
     log number is never reached. Verified independently against the two real
     ABC personal statements already in the ledger: their
     `parser_facts_sha256` are byte-identical before and after the change.
     The unlocked file parses cleanly at 72 rows. It belongs to ABC personal
     account `…7177`, and the book was not a judgement call after all —
     finance-desk's `personal.sqlite3` files it alongside `ccb-7564`,
     `abc-2061` and `mybank-7968`. It is admitted and imported: 165 new facts
     across two statements (164 + 72 rows, 71 of them the same facts seen
     twice).

     ~~The second file carries `商户` plus 14 digits where a 10-character log
     number belongs — one row in the whole file, itself overflowing by one
     character into 交易渠道. Admitting it means widening `_LOG_NUMBER`. The
     digest worry recorded earlier turns out not to apply: the encode would
     move from ascii to utf-8, and ascii is a subset of utf-8, so every
     existing 10-character log number hashes to the same bytes. The real
     question is semantic — it would mean accepting that the bank sometimes
     puts a merchant reference in the log-number column, and that value
     becomes the row's `transaction_serial` and part of its `fact_sha256`.
     That is a statement about what identifies a fact, so it is left to the
     user.~~ **Decided 2026-09-12 by the user: "算" — count it (`9853525`).**
     `_LOG_NUMBER` did not widen; a second form was named beside it,
     `_MERCHANT_REFERENCE`, pinned to `商户` plus exactly 14 digits, because
     the admission is about the shape the bank was actually seen to write
     rather than about loosening the column. `_repair_log_overflow` takes
     back the one character it spills into 交易渠道, and only when the two
     halves join into exactly that shape and the printed boundary shows no
     whitespace on either side — a channel carrying real text is left alone.
     Refusing the row was never worth one row: it cost the whole 164-row
     file. All four real ABC personal PDFs now parse, and the two already in
     the ledger keep their exact `parser_facts_sha256`.
4. ~~**WeChat cannot become a bank-statement profile.**~~ **Done 2026-09-12
   (`8176409`), by the path Core already had.** 2,212 transactions are in the
   ledger as PENDING review candidates.

   The user's instruction was that WeChat's balance should be treated as a
   normal account's, and screenshots of 零钱明细 and 零钱通明细 showed that
   WeChat does exactly that: a per-row running balance, kept properly. The
   correction is that *this document does not carry it*. Concretely, on
   2026-09-01 the app shows a transfer in and the immediate sweep into 零钱通;
   the export holds only the transfer. The daily 零钱通 interest appears in the
   app every day and in the export not once. So the bill is a payment record,
   not an account's record, and neither chain can be rebuilt from it. The user
   confirmed neither 明细 view can be exported.

   Interest is out of scope by the user's decision ("他们的利息收入忽略"). The
   consequence is worth stating: a balance computed from the ledger will drift
   below the real 零钱通 balance by roughly a yuan a day.

   What the export does carry is its own completeness proof - the record count,
   and the count and total of income, expenditure and direction-less rows. All
   three exports were checked against it and all three agree. That is what
   makes a balance-free import trustworthy, and it is enforced in
   `local_wechat.py` rather than assumed.

   Five things the build settled, each visible in the code:
   - 4 files, two byte-identical, so 3 exports; 3,228 rows, 2,212 distinct
     transactions after the overlap.
   - Where two exports differ, the money must agree and the description may
     not: 5 rows differed only in the counterparty's WeChat display name,
     which is their profile rather than a property of the payment. The newer
     export wins on description; a disagreement about time, direction, amount,
     type, funding or merchant reference stops the batch.
   - Nothing is classified. Every candidate is at zero confidence, 2,179 under
     `UNCLASSIFIED` and the 33 rows WeChat itself gives no direction under
     `INTERNAL_TRANSFER`, whose sign is left to review rather than inferred
     from the transaction type.
   - The manifest is derived entirely from the files, with no clock reading in
     it, because the batch receipt compares manifest digests and a timestamp
     would make every re-run look like a new batch.
   - 55 rows are funded by 建设银行(7564) and 农业银行(2061), whose statements
     are already imported. They are in, and unnetted: offsetting them is a
     ledger relationship, not something an importer should decide.

   **The replay wart, and what it actually was.** The first import of this
   batch was made with a manifest carrying a clock reading, and I recorded that
   as the reason its replay is broken. **That was only half right, and the
   other half is worse.** Building the same batch twice now produces a
   byte-identical *source* manifest — the clock fix works — but a different
   *prepared* manifest, because preparing it encrypts the evidence afresh and
   the envelope is not deterministic. The receipt compares both digests, so a
   second run of *any* candidate batch conflicts, not just this one. Measured
   directly on 2026-09-12:

   ```text
   source manifest    d2e1fe1a94879641 d2e1fe1a94879641 same
   prepared manifest  1ab9a6d76a6b0b6d 7d7ef31aed77a375 DIFFERENT
   ```

   So the candidate path is fail-closed on re-run rather than idempotent, and
   closing it means either comparing only `source_manifest_sha256` or making
   the envelope reproducible — both changes to a released import path in Core,
   so they are recorded here rather than made. Nothing is at risk: a re-run
   refuses, it does not double-import. See open issue 8.

   **Left as it stands, 2026-09-12, by the user: "留着不动".** The database is
   not being rebuilt for it.

5. **D-028's repeat-import relaxation is still not implemented for statement
   cutovers, and this is the concrete shape of the gap.** D-028 says a released,
   restore-verified import capability uses a bounded preview, transactional
   write, receipt, count check and idempotent replay for further files, and does
   not repeat the whole recovery rehearsal.

   Every statement import here still takes a full backup and rehearses a
   restore, which costs about five seconds a book — not the problem. The problem
   is replay. The gate wants one inventory to equal *both* the backup it is
   handed *and* the live counts minus the batch. Immediately after an import
   those are the same number, so re-running the manifest replays exactly and
   changes nothing — proved below. Once another book is imported on top, they
   are different numbers and no backup can be both, so an older manifest is
   refused. Fail-closed and honest, but it means the idempotent replay D-028
   asks for holds only for the most recent batch.

   Closing it means changing a gate on the path that writes real financial
   facts, so it is recorded rather than done.
6. **WeChat's two wallets are admitted, and hold nothing.** The user's
   "建" asked for 零钱 and 零钱通 as proper accounts, and they are:
   `wechat-lingqian` and `wechat-lingqiantong`, kind `WALLET`, in the
   personal book. Two rather than the single `wechat-0001` finance-desk
   models, because the screenshots show two independent running balances and
   one account cannot hold two. The export's own 支付方式 column agrees: it
   names 零钱 1,091 times and 零钱通 1,072, on equal footing with the bank
   cards beside them — among which are 建设银行(7564) and 农业银行(2061),
   accounts already in this book.

   Two things about the admission are worth stating plainly. WeChat gives
   these wallets no account number, and the registry requires four digits;
   `0001`/`0002` are an ordinal of ours, and there is deliberately no
   `SUFFIX` alias claiming they are a masked account number — the accounts
   are identified by `account_key`, and the one alias each carries is a
   `LABEL` holding the wallet's real name. The admission evidence is the
   newest WeChat export, the same file for both, which is honest: it is the
   document that names them.

   What they do not have is transactions. The bill export carries no balance
   chain, so nothing can enter them through the statement path, and the
   2,212 candidates are not bound to an account. Admitting them makes the
   book complete and gives review somewhere to put these facts; it does not
   by itself put anything there.
7. ~~**Alipay has no path into the ledger.**~~ **Done 2026-09-12 by the
   user's "入账" (`61a2963`).** 6,735 candidates, PENDING, in the personal book.

   Alipay's export is the same kind of document WeChat's is, so it took the
   same path; what was WeChat-shaped in `local_candidates.py` is now
   platform-shaped, with `local_payments.py` holding what a payment bill is and
   the batch manifest naming which platform wrote the file. No format sniffing:
   guessing wrong about a financial document is worse than being told.

   Three things the real files settled.

   - **Alipay does not stand behind its own amount totals, and says so.**
     Footnote 6 of every export: 因统计逻辑不同，明细金额直接累加后，可能会和
     下方统计金额不一致. Two of the three files bear it out — the expenditure
     rows exceed the declared total by ¥53,460.29 and ¥105,803.24. Enforcing
     those totals would refuse every Alipay file for being what it says it is.
     The *counts* are enforced, on all four figures, and all three files match
     exactly: that is the loss worth refusing, because a row that went missing
     changes a count. WeChat's totals reconcile to the cent and stay binding.
   - **A payment is identified by the account and the order number, not the
     order number alone.** The two Alipay logins transfer to each other, and 22
     transfers carry the same order number in both bills — once as money going
     out, once as money coming in. Those are two facts, one per account. The
     account is folded into the derived references as a digest, never in the
     clear: a login is an email address or a phone number. This surfaced as a
     real collision when the second batch was imported, not as a theory.
   - **The counterparty's own account is read past and never kept.** Alipay
     prints it; it is someone else's identifier and the ledger has no use for
     it.

   Two batches, one per login, because one batch may not mix two accounts —
   the reader takes the account from the preamble and the builder refuses files
   that name different ones. 1,856 candidates for `…8757` (two overlapping
   exports, 860 + 996 rows) and 4,879 for `…5002`.

   The two Alipay logins are **not** admitted as managed accounts. Unlike 零钱
   and 零钱通 the user did not ask for that, and as with WeChat the candidates
   bind to no account, so admitting them would add names and nothing else.
8. **Candidate batches fail closed on re-run rather than replaying.** The
   receipt compares `prepared_manifest_sha256`, and preparing a manifest
   encrypts the evidence afresh, so the digest differs every run even when the
   source manifest is byte-identical. Proved above under open issue 4. This is
   the candidate path's version of the D-028 gap that open issue 5 describes
   for statements: safe, honest, and not the idempotent replay D-028 asks for.
   Closing it is a change to a released import path in Core.
9. ~~**Four review views are mounted but disabled.**~~ **Done 2026-09-12 by the
   user's "开吧".** All four answer 200 against the real books.

   `candidate-events`, `candidate-classification-groups`,
   `company-transaction-classifications` and
   `company-transaction-classification-summary` are GET routes living inside
   command routers, and Core gates both routers on
   `enable_internal_candidate_command_api`. Local mode now sets it, with the
   database command backend so the classification groups come from the same
   books the candidates beside them do - a synthetic backend here would put
   fixture rows next to the user's own ledger with nothing on screen saying
   which was which.

   **The assertion key is minted per start and never written down.** `Settings`
   requires a key, an issuer and an audience before it will accept the module,
   because in production a command carries a signed assertion. Local mode
   serves no command, so nothing ever presents one and nothing ever verifies
   one: the key authorizes nothing, and a secret on disk that authorizes
   nothing is a liability with no compensating use. It is generated the same
   way the read cursor key already is, and dies with the process.

   *(Superseded 2026-09-13: local mode now serves two unsigned candidate
   decisions; see "Local review opened". The two paragraphs below record
   what was true on 2026-09-12.)*

   **Read-only did not become a promise.** `_read_routers` still takes only the
   GET routes out of those routers, and `_refuse_non_read_routes` still fails
   the start if a writer is ever among them - now with a test that walks the
   assembled app and asserts no route accepts anything but GET/HEAD/OPTIONS.
   `guard` also refuses an open `internal_candidate_command_operational_gate`,
   beside the read gate it already refused.

   Proved against the running local Core:

   ```text
   candidate-events                            200 {"items":[],...}
   candidate-classification-groups             200 {"items":[],...}
   company-transaction-classifications         200 {"items":[],...}
   company-transaction-classification-summary  200 items for entity 08519631...
   ```

   The first three are empty because nothing has been decided or grouped yet,
   which is the correct answer rather than a missing one. Note the summary's
   path: it is a sibling route, not `.../summary`, and it requires `from_date`
   and `to_date_exclusive`.

### Replay, proved 2026-09-12

`local_mode.py statements` re-run against the most recently imported book:

```text
reading 1 statements for 1 account(s)
replaying against backup: local-backup-20260912T032953Z
LOCAL_STATEMENTS_OK statements=1 created=0 replayed=1 transactions=89 review=PENDING
```

`bank_statement` / `bank_statement_transaction` / `bank_statement_observation` /
`audit_event` / `evidence_object` all unchanged at `60/2484/3073/5859/62`. The
same five counts are unchanged after three older manifests are re-run and
refused. This is the task's fourth acceptance test, met for the statement path
within the bound described in open issue 5.

## The whole stack, proved on the real books (2026-09-12)

Core on `127.0.0.1:8661` and the Web BFF on `127.0.0.1:8080`, both from this
branch, serving the imported books to a browser on this machine. The review
queue shows **8947 条待处理** with real WeChat and Alipay rows - real dates,
real amounts, real counterparties - and `本月候选营业单元 1 家 personal`. That is
the goal in the user's words ("让这个工作台在本地") met end to end, not in tests.

Three things the run taught, in decreasing order of how much they cost.

### `CORE_BUSINESS_UNIT_REF` is the unit's stable ref, not its UUID

`CORE_ENTITY_REF` is a UUID, so the unit ref looks like it should be one too.
It is not: Core scopes candidates by the short name the book was admitted
under, `book-08`. Pointed at the UUID, the BFF returned **0 candidates while
Core returned 100 for the same book**, and every request succeeded. An empty
queue is indistinguishable from a book with nothing left to review, so the
screen said the work was done while 8,947 candidates sat in it.

That is the one failure a ledger must never make quietly, so it is now a
refusal at startup rather than an empty page later: `_verify_local_scope` in
the Web repo asks Core once whether the ref names a book of this entity and
refuses with the refs that do exist when it does not. The same probe catches
local Core being down, which otherwise also arrives as a blank screen.
Committed on `ai/claude/web-local-single-user-mode` as `b34d780` with five
tests; that repo's Python suite is 184 passed / 1 skipped.

Two lesser traps from the same run, recorded because both cost time and
neither is discoverable from an error message: a **stale Core process** from an
earlier run of this task held 8661 and answered `503
INTERNAL_READ_UNAVAILABLE` from old code, and the BFF defaults `SITE_ROOT` to
`/site`, so it must be given the built `dist/` explicitly.

### The Web branch's TypeScript and vitest failures are not ours

8 `tsc` errors and 14 vitest failures, all in
`src/company-reports/CompanyReportsPage.tsx` and `src/App.test.tsx`. Both were
checked out at the parent commit `9e64b46` and both fail there identically, so
they are pre-existing and belong to the Codex web branch that owns those files.
Recorded rather than fixed: D-028 gives one writer per file.

### What the workbench gets wrong on screen

Observed, not yet diagnosed, and none of it touches stored facts:

- Every candidate is labelled **照片凭证** though its evidence is an XLSX or a
  CSV. The label is being chosen without consulting the evidence's media type.
- The header date is the **import time** (`9/12 16:48`), not the transaction
  time. The correct `accounting_month` renders below it, so the fact is right
  and the prominent number is wrong - the worse way round.
- The 公司账单确认 panel shows **LedgerBridge Core request failed / 重试公司流水**.
  Expected here: that panel reads the company book, and this profile is
  scoped to one personal book.
- The category renders untranslated as **Unclassified, pending review**.

## The branch carries Claude's identity now (2026-09-12, user: "重写")

Fourteen of this branch's twenty-two commits were authored and committed as
`Codex <codex@ledgerbridge.local>` while the git identity was misconfigured.
They are mine, and D-005 puts Claude's work on `ai/claude/*` under Claude's
name, so the range `668aee5..HEAD` was rewritten with `git filter-branch
--env-filter`, touching only author and committer.

Three things about it worth keeping:

- **The trees are byte-identical.** `git diff` between the pre-rewrite tip and
  the new one is empty. Only identities moved.
- **Nothing else pointed into the range.** No other branch contains these
  commits, and `878209d` - the one merged in from
  `ai/claude/boc-company-container-readers`, which has its own worktree - kept
  its exact hash because nothing about it changed. That branch is untouched.
- **The old tip is kept** at `refs/backup/local-single-user-mode-before-identity-rewrite`.

Nothing here was ever pushed, so no force-push and no coordination were needed.
The intake plans in the scratchpad record a `target_revision` of the old
hashes, and so do the registry rows those runs wrote; the content behind them
is unchanged and they are left as the historical record they are.

## A destructive mistake, recorded

A `local_mode.py verify` subcommand was written that ran the whole suite with
the local role URLs exported, and it was wrong twice over: it emptied
`ledgerbridge` (the schema survived at head; every row did not), and exporting
the `LEDGERBRIDGE_*` variables process-wide failed 117 tests that pass with a
clean environment. It has been removed; the local database was re-imported.

What stands from that exercise is the targeted run: the eight files holding
PostgreSQL integration tests pass in full against the local container - 178
tests, including all five privilege-boundary assertions - which is the evidence
that the local database enforces what Core's does rather than merely holding
the same rows.

## Review findings

-

## Rules carry-over (2026-09-14, user: "分类加「性质」" and "按规则分类整批确认")

What stands:

- `reporting_category.nature` (INCOME / EXPENSE / TRANSFER), migration
  `20260913_0052`. It can be set once from NULL; any other update is still
  refused by the append-only trigger. `get_accounting_dimensions` returns it.
- The personal summary (Core and the Web page) leaves TRANSFER categories out
  of income, expense, net, shares and monthly totals and reports transfers on
  their own line.
- The rules file lives only at `~/.ledgerbridge-local/rules/personal.json`,
  exported once from finance-desk's personal rules outside the repository.
  `local_mode.py categories --entity <uuid>` creates its categories.
- `GET /internal/v1/local/rule-suggestions` groups pending candidates by
  (category, rule). `POST /internal/v1/local/rule-batches/decisions` takes at
  most 200 members, re-matches each one against the current file and decides it
  through the existing audited decision path, with the rule id in the reason.
  The Web page 规则建议 confirms a group only after the user opens it and clicks.

Proof on the real books, with nothing decided:

- Backup `local-backup-20260913T191315Z` and its restore rehearsal passed first.
  Then the migration ran, and `categories` inserted 43 rows; a second dry run
  reported 0 to insert.
- Suggestions: 8,947 pending, 68 groups (7,526 transfer, 1,226 expense), 195
  unmatched. This matches the earlier coverage count.
- Batch probes through Core and through the BFF: a stale revision gave `STALE`,
  a changed rules version gave 409, and no CSRF token gave 403. The probed
  candidate stayed PENDING at revision 1.
- The Core suite, including PostgreSQL tests on a throwaway CI-shaped database,
  passed: 2988 passed, and the 72 skips are POSIX-only.

Still open:

- 195 unmatched candidates (mostly Alipay 收入) need individual review or new
  rules.
- The company rules (finance-desk COMPANY) are not carried over.
- The two old placeholder categories (`*_TRANSACTION_REVIEW`) have no nature.

Changed by the user on 2026-09-14 ("放进drive", then option 1): the rules file
may also be kept in the user's Google Drive, outside the `AI` workspace, so
work can continue on another computer. It still never enters the repository.
