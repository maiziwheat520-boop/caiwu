# Shared Financial Foundation

The Shared Financial Foundation preserves identity, account, evidence, money, and audit facts
used by independently maintained Personal Finance, Hotel Reconciliation, and Payroll modules.

## Language

**Managed Account**:
A personal or company account admitted to the account registry because the user provided at
least one statement for it.
_Avoid_: Internal account, known account

**Internal Transfer**:
A transfer between two Managed Accounts belonging to the same accounting owner. It requires
statement evidence from both sides before confirmation.
_Avoid_: Expense, income

**Related-Party Transfer**:
A transfer between Managed Accounts belonging to different companies or accounting owners.
It requires bilateral statement evidence and retains its inter-entity nature.
_Avoid_: Internal Transfer, automatic offset

**Bilateral Statement Evidence**:
Two source records from the sending and receiving Managed Accounts that show equal opposite
amounts in the same currency within the approved date window.
_Avoid_: Counterparty name match

**Bill Observation**:
Fields extracted from one source bill image, together with field confidence and blockers. It
is review input, not an approved accounting fact.
_Avoid_: Posted bill, confirmed transaction

**Evidence Object**:
An immutable, source-bound record proving where a financial observation came from.
_Avoid_: Attachment, mutable file

**Official Source Document**:
A document exported or issued by a bank, payment platform, employer, or other authoritative
provider and admitted only with its exact Evidence Object identity. It proves source content;
it does not by itself approve an accounting classification or posting.
_Avoid_: Trusted spreadsheet, posted transaction

**Normalized Financial Fact**:
An append-only structured record derived from an Official Source Document and bound to its
Accounting Owner, Managed Account, source location, and Evidence Object. It may feed several
modules without becoming a classified Ledger Draft or a Posted Entry.
_Avoid_: Imported row, bookkeeping result

**Ledger Draft**:
A balanced, evidence-linked journal proposal awaiting explicit human approval. Rules and models
may prepare or explain it but cannot turn it into a Posted Entry.
_Avoid_: Candidate, automatic posting

**Posted Entry**:
An immutable, human-authorized journal entry whose postings balance per currency and form the
formal accounting basis. Corrections use reversal or adjustment entries rather than mutation.
_Avoid_: Confirmed statement, report row

**Reporting Basis**:
The explicitly selected fact layer used by one report: review Candidate, account-statement cash
flow, or Posted Entry. Bases remain visibly separate and are never silently combined.
_Avoid_: Best available data, unified total

**Accounting Owner**:
The person or company whose books contain a Managed Account and its resulting facts.
_Avoid_: Login user, counterparty

**Business Unit Assignment**:
An optional, effective-dated default attribution of a Managed Account to one business unit.
Its absence keeps the account visible at Accounting Owner scope rather than implying a store.
_Avoid_: Account owner, permanent store binding

**Fact Allocation**:
An explicit business-unit allocation for one financial fact that takes precedence over the
Managed Account's effective-dated default and may split the fact across business units.
_Avoid_: Account ownership, inferred store

**Business Unit Snapshot**:
The immutable business-unit reference and label captured when an assignment or allocation is
made, preserving the historical meaning even if the current directory later changes.
_Avoid_: Live directory join, mutable label

**Account Registry Projection**:
A versioned, read-only owner-scoped view of Managed Accounts, aliases, effective assignments,
and fact allocations.
_Avoid_: Writable account table, inferred ownership

**Money Amount**:
A signed integer in the currency's smallest unit; for CNY the unit is one fen.
_Avoid_: Float amount, display yuan
