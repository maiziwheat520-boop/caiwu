# Task: browser original-reconciliation workflow

- Status: first vertical slice implemented; feature parity remains open
- Core branch: `ai/chatgpt/reconciliation-workflow-core-v1`
- Web branch: `ai/chatgpt/reconciliation-workflow-web-v1`
- Production data/deployment: not authorized and not performed

## Root cause

The deployed projection was empty for June and July because Core contained no facts for those
months.  The deployment scope was also pinned to a May review business unit, and the read-only
projection enumerated Candidate facts only; it never imported the historical workbook.  A
private, read-only workbook probe confirmed that both month sheets contain input values and
formulas.  Therefore an empty projection is not evidence of an empty month.

## Module and seam

`build_original_reconciliation_manifests(workbook_path, plan)` is the import Module's Interface.
The reviewed private plan is the seam between private workbook coordinates and the public Core
implementation.  The implementation owns exact money conversion, formula rejection,
deterministic Candidate/source/operation identities, evidence digest binding, month assignment,
and one controlled-import manifest per business unit.  Tests and callers cross the same seam.

Real labels, values, coordinates, Entity IDs, business-unit IDs, and category IDs stay outside
Git.  Tests use a synthetic workbook.  A formula cell cannot become a business item; it remains a
derived source fact.  `scripts/build_original_reconciliation_import.py` previews by default and
writes private bundles only with the explicit `--write-bundles` switch.  Existing controlled
prepare/import tooling remains the only database writer.

## Functional parity matrix

| Excel capability | Browser replacement target | First-slice status |
| --- | --- | --- |
| Import | Reviewed private mapping produces evidence-bound Core items | Partial: preview/bundle builder implemented; browser upload not exposed |
| Per-item view | Month-filtered item cards and detail dialog | Implemented for imported workbook Candidates |
| Automatic/manual match | Show proposals, require explicit selection, preserve match basis | Partial: existing Candidate classification/review can be opened; reconciliation match command remains open |
| Supplement/edit | Save a revision without silently changing formal ledger facts | Partial: existing correction-and-confirm path is available; separate draft-save action remains open |
| Category and description | Editable stable category plus human description | Partial: category correction exists; description revision remains open |
| Evidence link | Workbook evidence attached on import; add/remove links audited | Partial: import attachment implemented; manual link editing remains open |
| Submit review | Explicit transition from prepared item to reviewer queue | Partial: imported Candidates enter PENDING; separate preparer submission remains open |
| Confirm / return | Reviewer confirms or returns with a reason | Partial: confirm exists; return state/command remains open |
| Resubmit | Returned item can be revised and resubmitted | Open |
| Monthly summary / closure | Recompute from authoritative facts and publish immutable close | Open; the existing projection remains read-only and incomplete |
| Refresh / reopen persistence | Save, GET the same item again, verify revision/status | Implemented in Web for Candidate decisions |
| Export | Generate a review/closure artifact without treating it as the database | Open |

## First visible vertical slice

1. A reviewed mapping converts original input cells into controlled Candidate manifests.
2. Imported workbook Candidates appear as month-filtered item cards, not as an Excel grid.
3. Opening a card uses the existing evidence-backed Candidate detail and correction/review form.
4. After a decision is saved, Web re-reads the Candidate and verifies identity, revision, and
   status before claiming success.

This slice does not import the private workbook into production, deploy either branch, invent a
browser upload button, claim monthly closure, or turn formula totals into transactions.
