# LedgerBridge Financial Review

LedgerBridge preserves financial evidence and turns it into reviewable accounting candidates
without guessing missing facts or posting automatically.

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
