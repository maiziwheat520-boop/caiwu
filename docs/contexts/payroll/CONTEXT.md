# Payroll

Payroll owns employee pay calculation, locked approval versions, and verification that approved
amounts were disbursed; it never executes payment.

## Language

**Payroll Batch**:
A company- and pay-period-scoped set of employee pay lines with an immutable version identity.
_Avoid_: Payment file, bank transfer batch

**Locked Payroll Version**:
The approved Payroll Batch snapshot bound to the submit, independent review, and independent
approval audit chain.
_Avoid_: Latest draft, editable spreadsheet

**Disbursement Verification**:
A comparison between each locked employee net amount/account mapping and statement-backed receipt
evidence.
_Avoid_: Payment submission, bank execution

**Payroll Disbursement Record**:
A read-only payroll projection of an already-ingested Normalized Financial Fact whose persisted,
confirmed company classification is `PAYROLL`. Its source identity, ingestion time, classification
revision, and payroll-period assignment remain visible. It starts unlinked and must not be assigned
to an employee by amount alone.
_Avoid_: Re-parsed statement row, guessed employee payment

**Payroll Period Assignment**:
The versioned rule that assigns a Payroll Disbursement Record to a pay period. The current rule
assigns a bank transaction to the preceding month because regular payroll is paid in the following
month; exceptions remain reviewable instead of being silently shifted.
_Avoid_: Bank occurrence month, UI date guess

**Payroll Publication**:
A versioned, non-payable projection made available to LedgerBridge for accounting and
reconciliation only.
_Avoid_: Payment instruction, database sharing

## Ownership

The bank-ingestion and company-classification pipeline owns source parsing, normalized facts, and
the confirmed `PAYROLL` classification. Payroll owns the period-assigned read model and later
employee linkage. The payroll page only reads this projection; it neither opens source files nor
duplicates authoritative financial facts.
