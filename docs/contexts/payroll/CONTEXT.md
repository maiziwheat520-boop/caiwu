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

**Payroll Publication**:
A versioned, non-payable projection made available to LedgerBridge for accounting and
reconciliation only.
_Avoid_: Payment instruction, database sharing
