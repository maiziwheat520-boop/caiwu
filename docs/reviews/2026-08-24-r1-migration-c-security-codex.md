# Security Review: LedgerBridge R1 Migration C

## Scope

Independent diff review of R1 Migration C fact hardening, reader views/functions, grants, and downgrade controls.

- Scan mode: diff
- Target kind: git_diff
- Target ID: f38da88..fada207
- Revision range: f38da88...fada207
- Snapshot digest: codex-security-snapshot/v1:sha256:0000000000000000000000000000000000000000000000000000000000000000
- Inventory strategy: diff
- Included paths: alembic/versions/20260824_0014_r1_fact_hardening.py, alembic/versions/20260824_0015_r1_internal_read_surface.py
- Excluded paths: PROJECT_STATUS.md, docs/architecture/R1_DATABASE_SCHEMA_GRANTS.md, tests/
- Runtime or test status: Windows and disposable Hermes PostgreSQL 15 validation completed; no production database or credentials used.
- Artifacts reviewed: threat_model.md, in_scope_files.txt, candidate_ledger.jsonl, disposable PostgreSQL 15 replay evidence
- Scan context: TAC status was not_granted; protected scan outputs may be unavailable. This terminal report is source-backed and locally generated.

Limitations and exclusions:
- No live cross-tenant SELECT replay was run after the final scan because the disposable database was cleaned up.
- Excluded production databases and real ledger rows: Review used source and disposable PostgreSQL evidence only.
- Excluded ORM/read service and external mTLS verifier: Those components are not in the Migration C diff inventory.
- Excluded tests/ and documentation: Test and documentation changes were used as supporting evidence, not scanned as executable security surfaces.

### Scan Summary

| Field | Value |
| --- | --- |
| Reportable findings | 3 |
| Severity mix | high: 1, medium: 2 |
| Confidence mix | high: 2, medium: 1 |
| Coverage | complete |
| Validation mode | static source trace plus prior disposable integration replay |

Canonical artifacts: `scan-manifest.json`, `findings.json`, and `coverage.json`. This report is a deterministic projection of those files.

## Threat Model

R1 Migration C protects ledger facts, encrypted evidence lineage, audit horizons, reader authorization, role ACLs, and downgrade safety against compromised runtime roles and untrusted database callers.

### Assets

- ledger/evidence/reconciliation facts
- audit sequence/hash chain
- reader credential and database ACLs
- confidential cross-entity metadata

### Trust Boundaries

- runtime roles to PostgreSQL ACLs
- SECURITY DEFINER functions to public facts
- reader SQL inputs to as-of functions
- downgrade to persistent schema/ACL state

### Attacker Capabilities

- use a compromised reader/runtime credential
- issue direct SQL against granted objects
- submit malformed cursor/scope/blob/audit inputs
- exploit restore-time role drift

### Security Objectives

- prevent cross-entity disclosure
- preserve immutable fact/audit integrity
- fail closed on role and downgrade drift

### Assumptions

- production credentials and real data are out of scope
- the migration owner is the fixed database owner
- the external reader process is a separate deployment gate

## Findings

| Finding | Severity | Confidence | Detailed write-up |
| --- | --- | --- | --- |
| [Reader projection views bypass entity and as-of authorization](#finding-1) | high | high | inline below |
| [POSTED attribution enforcement is opt-in for legacy entries](#finding-2) | medium | high | inline below |
| [Backup role privilege drift is not fail-closed](#finding-3) | medium | medium | inline below |

### Confidence Scale

| Label | Meaning |
| --- | --- |
| high | Direct evidence supports the finding with no material unresolved blocker. |
| medium | Evidence supports a plausible issue, but material runtime or reachability proof remains. |
| low | Evidence is incomplete and the item is retained only for explicit follow-up. |

<a id="finding-1"></a>

### [1] Reader projection views bypass entity and as-of authorization

| Field | Value |
| --- | --- |
| Severity | high |
| Confidence | high |
| Confidence rationale | The source directly grants SELECT on views whose definitions select all rows, and security_barrier does not implement row authorization. |
| Category | authorization-bypass / cross-tenant data exposure |
| CWE | CWE-200, CWE-862 |
| Affected lines | alembic/versions/20260824_0015_r1_internal_read_surface.py:430-452, alembic/versions/20260824_0015_r1_internal_read_surface.py:645-752 |

#### Summary

The migration grants `ledgerbridge_reader` SELECT on eight projection views that have no entity, business-unit, horizon, or principal predicate. A caller holding the reader credential can enumerate cross-entity current-state facts without using the scoped as-of functions.

#### Root Cause

The migration treats security-barrier projection views as an authorization surface, but they are only query-shaping controls. Because the reader receives direct SELECT and the views lack row predicates, the database credential can read every entity and current state without the scoped functions' entity, cursor, or audit-horizon checks.

**Reader receives direct view SELECT** — `alembic/versions/20260824_0015_r1_internal_read_surface.py:430-436`

The reader credential is authorized to query every projection view directly, without supplying an entity or horizon parameter.

```sql
GRANT SELECT ON internal_read.candidate_current_v,
    internal_read.candidate_evidence_v, internal_read.evidence_metadata_v,
    internal_read.reconciliation_current_v,
    internal_read.reconciliation_blocker_v,
    internal_read.reconciliation_proposal_v,
    internal_read.reconciliation_suspense_v,
    internal_read.ledger_posted_total_v TO ledgerbridge_reader;
```

**Projection views select broad fact sets** — `alembic/versions/20260824_0015_r1_internal_read_surface.py:699-752`

The view definitions have no tenant or as-of predicate; security_barrier only constrains predicate pushdown and does not filter rows.

```sql
CREATE VIEW internal_read.candidate_evidence_v
WITH (security_barrier = true, security_invoker = false) AS
SELECT candidate_id, ordinal, evidence_ref, kind, media_type_snapshot,
       display_name_snapshot, download_available
  FROM public.candidate_evidence;

CREATE VIEW internal_read.evidence_metadata_v
WITH (security_barrier = true, security_invoker = false) AS
SELECT evidence_ref, entity_id, business_unit_id, media_type, display_name,
       plaintext_sha256, plaintext_size, created_at
  FROM public.evidence_object;
```

**Scoped functions exist but are bypassable** — `alembic/versions/20260824_0015_r1_internal_read_surface.py:774-817`

The parameterized function demonstrates the intended scoped/as-of authorization contract, but the separate direct view grants bypass it.

```sql
CREATE FUNCTION internal_read.list_candidates_as_of(
    p_entity_id uuid,
    p_business_unit_id uuid,
    p_status varchar(16),
    p_audit_horizon_sequence bigint,
    p_audit_horizon_hash bytea,
    p_last_created_at timestamptz,
    p_last_candidate_id uuid,
    p_limit integer
)
```

#### Validation

The grant list and representative view definitions were compared with the scoped function signatures and the threat model's tenant boundary.

Validation method: static source trace and grant/definition review

**Reader receives direct view SELECT** — `alembic/versions/20260824_0015_r1_internal_read_surface.py:430-436`

The reader credential is authorized to query every projection view directly, without supplying an entity or horizon parameter.

```sql
GRANT SELECT ON internal_read.candidate_current_v,
    internal_read.candidate_evidence_v, internal_read.evidence_metadata_v,
    internal_read.reconciliation_current_v,
    internal_read.reconciliation_blocker_v,
    internal_read.reconciliation_proposal_v,
    internal_read.reconciliation_suspense_v,
    internal_read.ledger_posted_total_v TO ledgerbridge_reader;
```

**Projection views select broad fact sets** — `alembic/versions/20260824_0015_r1_internal_read_surface.py:699-752`

The view definitions have no tenant or as-of predicate; security_barrier only constrains predicate pushdown and does not filter rows.

```sql
CREATE VIEW internal_read.candidate_evidence_v
WITH (security_barrier = true, security_invoker = false) AS
SELECT candidate_id, ordinal, evidence_ref, kind, media_type_snapshot,
       display_name_snapshot, download_available
  FROM public.candidate_evidence;

CREATE VIEW internal_read.evidence_metadata_v
WITH (security_barrier = true, security_invoker = false) AS
SELECT evidence_ref, entity_id, business_unit_id, media_type, display_name,
       plaintext_sha256, plaintext_size, created_at
  FROM public.evidence_object;
```

**Scoped functions exist but are bypassable** — `alembic/versions/20260824_0015_r1_internal_read_surface.py:774-817`

The parameterized function demonstrates the intended scoped/as-of authorization contract, but the separate direct view grants bypass it.

```sql
CREATE FUNCTION internal_read.list_candidates_as_of(
    p_entity_id uuid,
    p_business_unit_id uuid,
    p_status varchar(16),
    p_audit_horizon_sequence bigint,
    p_audit_horizon_hash bytea,
    p_last_created_at timestamptz,
    p_last_candidate_id uuid,
    p_limit integer
)
```

Assertions:
- All eight views are directly granted to ledgerbridge_reader.
- Representative candidate/evidence views contain no entity, business-unit, horizon, or principal predicate.
- security_barrier does not itself create row-level authorization.

Limitations:
- A live cross-entity SELECT replay was not repeated during this review because the disposable database had already been destroyed; the SQL definitions are deterministic evidence.

#### Dataflow

reader login -\> direct projection-view SELECT -\> public fact rows -\> response

- **Source:** any process or operator holding ledgerbridge_reader credentials

- **Sink:** direct SELECT on internal_read projection views

- **Outcome:** cross-entity metadata enumeration without as-of authorization

**Reader receives direct view SELECT** — `alembic/versions/20260824_0015_r1_internal_read_surface.py:430-436`

The reader credential is authorized to query every projection view directly, without supplying an entity or horizon parameter.

```sql
GRANT SELECT ON internal_read.candidate_current_v,
    internal_read.candidate_evidence_v, internal_read.evidence_metadata_v,
    internal_read.reconciliation_current_v,
    internal_read.reconciliation_blocker_v,
    internal_read.reconciliation_proposal_v,
    internal_read.reconciliation_suspense_v,
    internal_read.ledger_posted_total_v TO ledgerbridge_reader;
```

**Projection views select broad fact sets** — `alembic/versions/20260824_0015_r1_internal_read_surface.py:699-752`

The view definitions have no tenant or as-of predicate; security_barrier only constrains predicate pushdown and does not filter rows.

```sql
CREATE VIEW internal_read.candidate_evidence_v
WITH (security_barrier = true, security_invoker = false) AS
SELECT candidate_id, ordinal, evidence_ref, kind, media_type_snapshot,
       display_name_snapshot, download_available
  FROM public.candidate_evidence;

CREATE VIEW internal_read.evidence_metadata_v
WITH (security_barrier = true, security_invoker = false) AS
SELECT evidence_ref, entity_id, business_unit_id, media_type, display_name,
       plaintext_sha256, plaintext_size, created_at
  FROM public.evidence_object;
```

#### Reachability

The reader role is explicitly intended for an internal process and is granted SELECT; any compromise or query bug in that process can issue the direct SELECT.

- **Attacker:** compromised internal-read process or operator with reader credentials

- **Entry point:** SQL SELECT against an allowlisted projection view

- **Outcome:** candidate, evidence, reconciliation, and posted-total metadata from other entities is returned

#### Severity

**High** — A compromised or misconfigured internal reader process can directly exfiltrate candidate, evidence, reconciliation, and posted-ledger metadata across entities; exploitation requires the reader credential but no additional database privilege.

Severity decreases if the reader role is provably confined to a single tenant database or all projection views are removed from the credential and only scoped functions remain.

#### Remediation

Remove direct view SELECT from the external reader role, or replace each view with a parameterized SECURITY DEFINER function that enforces entity/business-unit scope and an exact audit horizon. Add a regression that a reader credential cannot enumerate a second entity through any projection surface.

Tests:
- Create two entities and prove the reader cannot obtain entity B rows while authorized for entity A.
- Attempt direct SELECT on every projection view and require denial or a scoped policy.

Preventive controls:
- Treat security_barrier as an optimizer/injection control, never as tenant authorization.
- Review every reader grant against an explicit row-scope contract.

<a id="finding-2"></a>

### [2] POSTED attribution enforcement is opt-in for legacy entries

| Field | Value |
| --- | --- |
| Severity | medium |
| Confidence | high |
| Confidence rationale | The trigger source explicitly returns when attribution count is zero, and the companion posting trigger has the same opt-in condition. |
| Category | authorization/integrity enforcement gap |
| CWE | CWE-284 |
| Affected lines | alembic/versions/20260824_0014_r1_fact_hardening.py:2537-2577 |

#### Summary

The hardening trigger returns successfully when a POSTED journal entry has zero R1 attribution rows, so legacy writers can create POSTED entries without R1 scope or posting-category attribution.

#### Root Cause

The trigger uses presence of an attribution row as an opt-in marker rather than enforcing the R1 completeness invariant for every POSTED entry. This preserves legacy Core compatibility but leaves a state that is POSTED in the core ledger and absent from R1 scope facts.

**Zero attribution rows bypass completeness** — `alembic/versions/20260824_0014_r1_fact_hardening.py:2553-2564`

A POSTED entry with no R1 attribution exits before the exact-one and posting-attribution checks run.

```sql
SELECT count(*) INTO v_count
  FROM public.journal_entry_attribution WHERE entry_id = v_entry;
IF v_count = 0 THEN
    RETURN NEW;
END IF;
IF v_count <> 1 THEN
    RAISE EXCEPTION 'POSTED journal entry requires exactly one attribution';
END IF;
```

#### Validation

The trigger branch was inspected and the Hermes PostgreSQL full replay confirmed legacy Core POSTED tests continue to pass without inserting R1 attribution rows.

Validation method: static trigger control review plus compatibility replay

**Zero attribution rows bypass completeness** — `alembic/versions/20260824_0014_r1_fact_hardening.py:2553-2564`

A POSTED entry with no R1 attribution exits before the exact-one and posting-attribution checks run.

```sql
SELECT count(*) INTO v_count
  FROM public.journal_entry_attribution WHERE entry_id = v_entry;
IF v_count = 0 THEN
    RETURN NEW;
END IF;
IF v_count <> 1 THEN
    RAISE EXCEPTION 'POSTED journal entry requires exactly one attribution';
END IF;
```

Assertions:
- Zero attribution returns NEW.
- Rows with one or more attribution rows remain subject to exact-one and posting-category checks.

Limitations:
- The current reader functions join R1 attribution facts, so impact outside the R1 surface was not measured.

#### Dataflow

Core post operation -\> journal status POSTED -\> deferred trigger count=0 -\> commit

- **Source:** existing Core writer that does not populate R1 attribution

- **Sink:** r1_validate_posted_entry_completeness

- **Outcome:** unscoped POSTED entry persists

**Zero attribution rows bypass completeness** — `alembic/versions/20260824_0014_r1_fact_hardening.py:2553-2564`

A POSTED entry with no R1 attribution exits before the exact-one and posting-attribution checks run.

```sql
SELECT count(*) INTO v_count
  FROM public.journal_entry_attribution WHERE entry_id = v_entry;
IF v_count = 0 THEN
    RETURN NEW;
END IF;
IF v_count <> 1 THEN
    RAISE EXCEPTION 'POSTED journal entry requires exactly one attribution';
END IF;
```

#### Reachability

Any existing Core posting path that has not been migrated to R1 attribution can reach the bypass; the new R1 reader currently omits those rows.

- **Attacker:** authorized Core writer or compromised writer

- **Entry point:** journal POSTED transition

- **Outcome:** a POSTED row lacks R1 scope/category facts

#### Severity

**Medium** — The gap weakens the database invariant for legacy Core writes, although the new R1 reader joins require attribution and therefore exclude these rows from the R1 projections.

Severity increases if any downstream report treats all POSTED core entries as R1-complete or if a future reader path omits the attribution joins.

#### Remediation

Choose an explicit compatibility boundary: either backfill and require attribution for every POSTED entry before enabling R1, or mark legacy entries with a durable migration state and prevent any consumer from treating them as R1-complete. Add a test proving the chosen boundary.

Tests:
- Post an entry with zero attribution and assert the documented compatibility outcome.
- Verify every R1 reader/report excludes or separately labels legacy unattributed POSTED entries.

Preventive controls:
- Do not silently use presence of a child row as an authorization opt-in without a persisted migration state.
- Keep R1 reader joins and ledger totals consistent with the chosen legacy policy.

<a id="finding-3"></a>

### [3] Backup role privilege drift is not fail-closed

| Field | Value |
| --- | --- |
| Severity | medium |
| Confidence | medium |
| Confidence rationale | The omission is direct in source, but exploitability depends on whether a privileged backup role is deployed in the target environment. |
| Category | improper privilege management |
| CWE | CWE-269, CWE-732 |
| Affected lines | alembic/versions/20260824_0015_r1_internal_read_surface.py:176-231 |

#### Summary

The database ACL routine allowlists and grants CONNECT to `ledgerbridge_backup` when present, but the role is excluded from runtime preflight checks for inheritance, memberships, ownership, and privileged flags.

#### Root Cause

The backup role is treated as a trusted allowlist exception, while the preflight's controlled-role checks cover only reader, API, worker, and app. A backup role that inherits a privileged role can therefore pass Migration C without the same fail-closed drift checks.

**Backup role is granted database access** — `alembic/versions/20260824_0015_r1_internal_read_surface.py:176-231`

A present backup role is deliberately preserved and granted CONNECT, but this block does not validate its role flags or membership.

```sql
v_allowlist text[] := ARRAY[
    current_user, 'pg_database_owner',
    'ledgerbridge_reader', 'ledgerbridge_api',
    'ledgerbridge_worker', 'ledgerbridge_app',
    'ledgerbridge_backup'
];
...
EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', v_database, role_name);
```

#### Validation

The preflight role list and ACL allowlist were compared for backup-role coverage.

Validation method: static role/ACL control trace

**Backup role is granted database access** — `alembic/versions/20260824_0015_r1_internal_read_surface.py:176-231`

A present backup role is deliberately preserved and granted CONNECT, but this block does not validate its role flags or membership.

```sql
v_allowlist text[] := ARRAY[
    current_user, 'pg_database_owner',
    'ledgerbridge_reader', 'ledgerbridge_api',
    'ledgerbridge_worker', 'ledgerbridge_app',
    'ledgerbridge_backup'
];
...
EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', v_database, role_name);
```

Assertions:
- ledgerbridge_backup appears in the database ACL allowlist and CONNECT grant loop.
- ledgerbridge_backup does not appear in controlled_roles or membership/object-ownership validation.

Limitations:
- No production backup role was inspected; exploitability is deployment-dependent.

#### Dataflow

role drift -\> ACL allowlist exception -\> CONNECT and inherited privileges

- **Source:** deployed ledgerbridge_backup role with stale membership

- **Sink:** database and inherited object privileges

- **Outcome:** backup credential can exceed the intended restore-only boundary

**Backup role is granted database access** — `alembic/versions/20260824_0015_r1_internal_read_surface.py:176-231`

A present backup role is deliberately preserved and granted CONNECT, but this block does not validate its role flags or membership.

```sql
v_allowlist text[] := ARRAY[
    current_user, 'pg_database_owner',
    'ledgerbridge_reader', 'ledgerbridge_api',
    'ledgerbridge_worker', 'ledgerbridge_app',
    'ledgerbridge_backup'
];
...
EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', v_database, role_name);
```

#### Reachability

Requires a deployed backup role with privilege drift and compromise of that credential.

- **Attacker:** attacker with backup credential

- **Entry point:** database login after Migration C

- **Outcome:** inherited owner/table capability may remain available

#### Severity

**Medium** — A stale backup role can retain inherited owner or table authority through role membership while the migration reports success; exploitation requires deployment drift or a compromised backup credential.

Severity decreases if the deployment verifier proves backup-role attributes and memberships independently before migration, or if backup is removed from the migration allowlist.

#### Remediation

Either remove `ledgerbridge_backup` from the migration ACL allowlist and manage it in a separate restore verifier, or validate it with the same NOLOGIN/NOINHERIT, unprivileged, no-membership, and no-object-ownership checks before granting CONNECT.

Tests:
- Bootstrap a backup role with owner membership and require Migration C to fail.
- Bootstrap a clean backup role and verify it receives CONNECT only.

Preventive controls:
- Keep backup credentials outside runtime role allowlists unless their privilege contract is verified.
- Audit pg_auth_members and role flags during restore verification.

## Reviewed Surfaces

| Surface | Risk Area | Outcome | Notes |
| --- | --- | --- | --- |
| Reader projection views and grants | tenant authorization and data exposure | Reported | Unscoped direct SELECT grants produced R1-C-SEC-001. |
| Runtime role preflight and database ACL | privilege drift and role membership | Reported | Backup-role validation gap produced R1-C-SEC-002; reader/API/worker/app checks were otherwise reviewed. |
| Fact hardening and deferred trigger validators | ledger/evidence integrity | Reported | Legacy POSTED attribution opt-in produced R1-C-SEC-003; typed audit and lineage checks were reviewed. |
| As-of cursor, resolver, and receipt functions | horizon, cursor, blob, and audit validation | No issue found | Parameter validation, exact horizon rows, cursor scope, active-tip checks, and typed receipt binding were traced. |
| Downgrade and cleanup guards | destructive migration and residual ACLs | No issue found | Non-empty downgrade guards and explicit function/schema/table cleanup were reviewed. |

## Open Questions And Follow Up

- Is ledgerbridge_reader intentionally a global cross-entity reporting credential, or must tenant isolation be enforced in PostgreSQL?
  - Follow-up prompt: Decide whether to remove direct projection-view SELECT and require entity-scoped functions before production reader bootstrap.
- What exact privilege and membership contract applies to ledgerbridge_backup?
  - Follow-up prompt: Provide the restore-role manifest and validate it against Migration C preflight.
