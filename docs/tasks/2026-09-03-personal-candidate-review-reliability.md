# Personal candidate review reliability

Status: locally verified; release review in progress
Branch: `ai/chatgpt/personal-candidate-review-reliability`
Production baseline: Core `fdf8568648e96ab94226328b7750048b2569a118`, schema `20260903_0036`

## Outcome

Restore candidate detail, review-event, and similarity-group reads when the Personal Finance
workload's explicitly authorized candidate collection spans more than one business unit. Public
candidate pagination remains bound to one explicit business unit, as used by the Web BFF. The
change remains read-only: it does not classify, confirm, create journal entries, or post.

## Scope and invariants

- Read each exact Entity/business-unit grant at one shared audit horizon and maintain an independent
  keyset boundary for every scope.
- Preserve the public signed cursor's single-scope binding and revalidate every returned row against
  its source scope.
- Fetch evidence-risk satisfactions and counterparty facts in bounded batches of at most 101 IDs.
- Keep explicit `business_unit` filtering and unassigned-candidate scope fail closed.
- Do not change company reporting, payroll, reconciliation, migrations, or production data.

## Verification

The release gate must cover single-scope public pagination, multi-scope detail behavior, candidate
events and similarity groups, authorization regression, the complete relevant Core suite, exact
production revision, service health, and read-only production probes of the previously failing
endpoints.

The focused candidate read, decision-event, similarity-group, and cursor suites pass 87 tests.
Changed-file Ruff lint/format and mypy pass. The regressions exercise 101+ candidates in each of two
scopes, prove that a boundary from one scope is never reused in another, cap enrichment queries at
101 IDs, merge multi-scope event/group results without relaxing authorization, and prove that
classification batches and individual candidate decisions remain fail closed when the principal has
more than one candidate scope.
