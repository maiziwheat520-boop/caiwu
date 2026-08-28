# May financial foundation runbook

## Outcome

One run converts the supplied WeChat Pay, Alipay, and Bank of China evidence into a concise
monthly reconciliation workbook. The first worksheet is the review summary. Transaction,
account, transfer, and missing-evidence details remain on separate audit worksheets.

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
are excluded; explicit refund rows are treated as refund inflows. Calculations use integer cents
before worksheet values are written.

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
- A suspected same-holder transfer with only one side present goes to `待补佐证`.

## Workbook review surface

The workbook contains five sheets:

1. `五月对账`: concise totals, source coverage, transfer-match count, and missing-evidence count.
2. `五月流水`: normalized audit detail.
3. `账户台账`: supplied and referenced accounts with evidence status.
4. `内部转账`: bilateral matches and duplicate-evidence links.
5. `待补佐证`: only the missing statements that block an automatic match.

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

## Faster iteration rule

Do not rebuild the workflow as a sequence of ad-hoc PowerShell prompts. Keep source discovery,
normalization, checks, workbook generation, and manifest output behind the single runner above.
When a source format changes, add one parser adjustment and one synthetic regression case, then
rerun the same acceptance sequence. This is the project's **data ingestion pipeline**: a stable
path that turns different exports into one reviewable financial model.
