# May financial foundation runbook

## Outcome

One run converts the supplied WeChat Pay, Alipay, and Bank of China evidence into a concise
monthly reconciliation workbook. The first worksheet is the review summary. Transaction,
account, transfer, and missing-evidence details remain on separate audit worksheets.

The formal finance intake boundary starts at `2026-01-01`. The runner rejects earlier periods.
Current supplied rows are real-data test records: they remain pending review and are never
treated as approved or posted merely because a match was found.

The workflow never posts ledger entries. Every result remains pending human confirmation in
LedgerBridge Web.

## Inputs

- One or more WeChat Pay annual export workbooks named `wechat_*.xlsx`.
- One or more Alipay annual export files named `alipay_*.csv`.
- The controlled Bank of China review workbook containing the selected month's
  `<yy>.<month>中行邮箱待复核` worksheet.
- Optional screenshot evidence is preprocessed by the offline OCR workflow documented in
  `OCR_AND_MANAGED_ACCOUNT_REVIEW.md`; OCR output is not allowed to guess a cropped date.

Raw financial files stay in an ignored private staging directory. They must never be committed.

## One-command build

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/run_financial_foundation.ps1 `
  -InputDirectory <private-input-directory> `
  -BocWorkbook <controlled-boc-workbook.xlsx> `
  -Output <output-directory\may-financial-foundation.xlsx> `
  -Year 2026 `
  -Month 5
```

The command discovers all matching platform exports, filters the requested month, produces the
workbook and a machine-readable `.manifest.json`, and fails if a record id, evidence reference,
or internal-transfer invariant is invalid.

## Normalization rules

Every source record becomes one normalized row with:

- date, source, and source account;
- signed integer minor-unit amount;
- income/outflow/no-effect direction;
- counterparty, payment channel, state, and short evidence reference;
- whether the row contributes to the monthly result;
- a bounded review reason when the row cannot be finalized automatically.

Closed transactions and platform `不计收支` rows do not affect the result. WeChat full refunds
are excluded. A uniquely corresponding partial-refund row is linked to its original payment,
both evidence rows remain visible, and the result contains only their net expense. Ambiguous
refunds remain unmatched and in risk review. Calculations use integer cents before worksheet
values are written.

## Account and transfer rules

An account is a **Managed Account** only after its own statement evidence has been supplied.
Accounts merely referenced as a payment method or same-holder counterparty are registered as
`待补独立流水` and cannot be automatically cleared.

- Same-owner movement between two Managed Accounts is an internal transfer.
- Movement between different supplied company accounts is a related-party transfer.
- Both require equal-and-opposite bilateral statement evidence within the allowed date window.
- A platform purchase funded directly by a bank card is one economic transaction with two pieces
  of evidence. The platform row contributes to the result and the matching bank row is retained as
  evidence only, preventing double counting.
- A transfer routed through a platform and funded directly by a bank card uses the opposite
  source precedence: the bank row contributes to the result and the platform row is channel
  evidence only. This choice is based on transaction kind, never amount alone.
- A card suffix shown by WeChat or Alipay may differ from the deposit-account suffix printed on
  the bank statement. The runner only treats it as a funding-instrument alias when institution,
  signed amount, platform payment rail, and transaction date (within two days) form a one-to-one
  match. A competing card from another institution is never used to satisfy the match.
- Alipay `花呗`, `余额宝`, and `账户余额`, plus WeChat `零钱` and `零钱通`, are
  platform-internal subaccounts covered by the supplied complete platform statement. They never
  create an independent-statement request. Internal recharge, redemption, and balance movement
  remain inside that platform evidence boundary.
- Huabei purchases remain expenses at purchase time; Huabei repayment is a balance settlement,
  not a second expense. A real bank or external account explicitly named as the funding rail still
  requires the corresponding bank statement.
- Credit-card purchases remain expenses at purchase time. Their later repayment is a balance
  settlement, not a second expense. Until the complete credit-card statement and repayment-account
  statement are supplied, those rows stay in evidence-required review.
- Every hotel-platform settlement or withdrawal must be linked to the corresponding receiving-bank
  credit. The match must be one-to-one on exact amount, platform/payment-provider clue, and a bank
  credit dated zero to seven days after the settlement period. A clear OCR read is not enough to
  approve it; an unmatched payout remains in the evidence-required queue. A successful link only
  removes that material reminder and does not approve or post either candidate.
- A suspected same-holder transfer with only one side present goes to `待补佐证`.
- Internal/related classification never comes from words such as `银行`, `账户`, `转账`,
  or `投资理财` in a summary. The counterparty must be classified through the managed-account
  and business-counterparty registries as `self_managed`, `related_party`,
  `known_business`, or `unknown`. The safe default is ordinary
  transfer review. Self-managed and related-party transfer status additionally requires bilateral
  statement evidence.
- Normalized platform output carries a stable `counterpartyRef` and a
  `counterpartyClass`. Unicode/whitespace normalization lets repeated observations reuse the
  same explicit registry identity, while each transaction and evidence row stays independent.
  Platform-internal subaccounts have no external counterparty projection.
- The controlled-import schema persists normalized counterparty references and classifications in
  append-only Candidate/Counterparty facts. Stable identity is separate from versioned
  classification, so an initial `unknown` can later be explicitly classified without rewriting
  history. Core reads the latest classification visible at its audit horizon; absent or unknown
  identities keep the safe ordinary-transfer review path.

## Refund risk closure

The workbook matcher is a fail-closed projection: it links a partial refund only when the original
payment and refund are unique on platform account, counterparty, description, stated refund amount,
and date window. Core already accepts an audited `REVERSAL_MATCH_REQUIRED` satisfaction when
projecting review risks.

Production import persists that satisfaction as an append-only candidate-evidence relation at the
same controlled-import boundary used by hotel payout evidence. Both the original payment and refund
remain as Candidate/evidence facts; only a reciprocal, unique partial-refund relation satisfies
their separate `REVERSAL_MATCH_REQUIRED` risks. Missing or ambiguous matches remain fail-closed.

## Workbook review surface

The workbook contains five sheets:

1. `五月对账`: concise totals, source coverage, transfer-match count, and missing-evidence count.
2. `五月流水`: normalized audit detail.
3. `账户台账`: supplied and referenced accounts with evidence status.
4. `内部转账`: bilateral matches and duplicate-evidence links.
5. `待补佐证`: the exact missing statements/bills, affected period, transaction count, affected
   expense amount, and requested file type.

The machine-readable manifest contains the same list in `materialsNeeded`. The PowerShell runner
prints every item after a successful run, so an operator never has to infer missing materials from
the workbook manually.

Account lifecycle is explicit. The Agricultural Bank of China account ending in `2061` is
`closed` and `excluded_from_collection`: its historical transactions and evidence remain in the
ledger, but it is not emitted in `materialsNeeded`. This exception is keyed to that concrete bank
account only and does not affect any other Agricultural Bank account.

Core candidate risks do not yet carry a managed-account lifecycle fact. The persistence migration
point is the managed-account registry: project a structured `excluded_from_collection` value into
funding-evidence risk derivation, never infer it from a transaction summary or bank-name keyword.

The summary deliberately excludes attachments, raw messages, OCR diagnostics, and long source
descriptions. Those belong to the evidence view, not the review decision surface.

## Acceptance checks

- source row counts equal the filtered source counts;
- duplicate normalized record ids = 0;
- missing evidence references = 0;
- every internal-transfer pair has a positive amount and two evidence rows;
- workbook formula scan contains no `#REF!`, `#DIV/0!`, `#VALUE!`, `#NAME?`, or `#N/A`;
- every worksheet is rendered and visually checked before release;
- replaying the same inputs produces the same record ids and counts.
- the source totals equal the included rows plus evidence-only rows after economic-transaction
  merging;
- every `materialsNeeded` item names a period, count, requested evidence, priority, and status.
- the hotel cutover sidecar replaces only uniquely corresponding weekly summaries, never reuses a
  bank credit, and leaves every unmatched OCR or legacy hotel candidate in risk review.

## Least-privilege production import

The generated review bundle is imported by the regular `aiadmin` operations account. The import
must not require `sudo`, an interactive password, or a desktop approval prompt. This lets an Agent
run the already-authorized job through non-interactive SSH while the existing administrative
boundary remains intact.

The import runner must:

- verify the deployed revision, source-manifest digest, and backup-tool digest before starting;
- copy the three private source files into a uniquely named ephemeral Docker volume;
- keep the volume directory at `0700` and files at `0600`, owned by the container's non-root UID;
- resolve the evidence-key source from the deployed Core review configuration and require a
  readable regular file, never a directory or guessed path;
- run an agent-executable preflight against the exact preparation container mounts;
- create an encrypted backup and pass an isolated restore rehearsal before importing;
- import the prepared manifest twice and require the second result to be an idempotent replay;
- verify database counts, per-source counts, and container health;
- create and rehearse a second encrypted backup after the import; and
- remove the ephemeral volume on success or failure.

Any operation that genuinely changes host administrator policy remains a human authorization
step. Do not use Docker access to bypass that policy; design routine imports so they do not need
host administrator privileges.

## Faster iteration rule

Do not rebuild the workflow as a sequence of ad-hoc PowerShell prompts. Keep source discovery,
normalization, checks, workbook generation, and manifest output behind the single runner above.
When a source format changes, add one parser adjustment and one synthetic regression case, then
rerun the same acceptance sequence. This is the project's **data ingestion pipeline**: a stable
path that turns different exports into one reviewable financial model.
