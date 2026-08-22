# Security Review: ledgerbridge-phase3-security-scan-final-b72b229

## Scope

Final remediation range b1792701..b72b229 reviewed as a whole.

- Scan mode: branch_diff
- Target kind: git_diff
- Target ID: target_sha256_8e9b004d20dfe8a1e6ef0375f147c18261253be08a8a011e0c88882a2a75e036
- Revision range: b1792701fa20a55de6233206fbe29ce6ee427e28...b72b229363f60de71c19933c45a7ef8bc45ee346
- Snapshot digest: codex-security-snapshot/v1:sha256:1c162bb3e1e96327aaa00d104d05074dc38334a2c5c851113a0895348109e749
- Inventory strategy: diff
- Included paths: .
- Excluded paths: none
- Runtime or test status: Production unchanged; temporary Hermes only.
- Artifacts reviewed: 79 changed/reviewable files, local tests, Hermes Alembic 20260822_0004, Hermes direct probes

Limitations and exclusions:
- Hermes full pytest install blocked by isolated DNS and missing dev wheels.
- Excluded same-UID inode identity separation: authorized Slice A boundary

### Scan Summary

| Field | Value |
| --- | --- |
| Reportable findings | 1 |
| Severity mix | low: 1 |
| Confidence mix | high: 1 |
| Coverage | partial |
| Validation mode | static diff, local tests, Hermes migration and behavior probes |

Canonical artifacts: `scan-manifest.json`, `findings.json`, and `coverage.json`. This report is a deterministic projection of those files.

## Threat Model

Review Phase 3 Slice A against compromised runtime role, malicious archive supplier, connector input, and same-UID artifact writer. Protect ledger integrity, evidence provenance, recovery authenticity, least privilege, and artifact identity.

### Assets

- ledger rows
- raw artifacts
- ImportJob provenance
- audit chain
- restore host

### Trust Boundaries

- connector input -\> validator
- runtime role -\> PostgreSQL
- archive -\> restore
- filesystem -\> parser

### Attacker Capabilities

- submit connector metadata
- issue allowed SQL
- supply accepted archive
- write same-UID artifact bytes

### Security Objectives

- fail closed on substitution
- bind imports to audit
- restore known baselines
- reserve identities
- pin verifier

### Assumptions

- migration owner and running verifier trusted
- production excluded
- Slice B runner absent

## Findings

| Finding | Severity | Confidence | Detailed write-up |
| --- | --- | --- | --- |
| [Same-UID published inode remains mutable after digest verification](#finding-1) | low | high | inline below |

### Confidence Scale

| Label | Meaning |
| --- | --- |
| high | Direct evidence supports the finding with no material unresolved blocker. |
| medium | Evidence supports a plausible issue, but material runtime or reachability proof remains. |
| low | Evidence is incomplete and the item is retained only for explicit follow-up. |

<a id="finding-1"></a>

### [1] Same-UID published inode remains mutable after digest verification

| Field | Value |
| --- | --- |
| Severity | low |
| Confidence | high |
| Confidence rationale | Prior behavior proof remains valid; final diff adds no different-UID runner. |
| Category | CWE-367 |
| CWE | CWE-367 |
| Affected lines | scripts/backup_restore.py:809-853, docs/tasks/2026-08-22-phase-3-platform-security.md:360-372 |

#### Summary

A same-UID writer can change an opened artifact inode after hashing and before parser consumption.

#### Validation

Residual deferred to Slice B.

Validation method: Prior TOCTOU proof plus scope review

#### Dataflow

descriptor hash -\> mutable inode -\> parser

#### Reachability

Same-UID writer can race the open descriptor.

#### Severity

**Low** — Requires same-UID write access and is outside authorized Slice A.

Additional runtime or deployment evidence could raise or lower this severity.

**Impact assessment:** medium

**Likelihood assessment:** medium

#### Remediation

Implement Slice B different-UID Unix-socket runner or immutable descriptor-backed snapshot, with hostile-container and IPC proof.

Tests:
- Same-UID mutation cannot alter parser-consumed bytes.
- Runner has no database/artifact/OAuth/network access.

## Reviewed Surfaces

| Surface | Risk Area | Outcome | Notes |
| --- | --- | --- | --- |
| artifact archive validation | not recorded | No issue found | No additional canonical notes were recorded. |
| restore security baseline | not recorded | No issue found | No additional canonical notes were recorded. |
| runtime provenance | not recorded | No issue found | No additional canonical notes were recorded. |
| connector namespace | not recorded | No issue found | No additional canonical notes were recorded. |
| verifier pinning | not recorded | No issue found | No additional canonical notes were recorded. |

## Open Questions And Follow Up

- Slice B must provide different-UID runner and hostile IPC proof.
- different-UID runner is Slice B
  - Follow-up prompt: Review deferred unit candidate-eed54f9ff851d097 and close its stated proof gap.
