# Task: Dedicated multi-company company-report principal

- Status: implementation and local validation complete; activation owned by central integration
- Date: 2026-09-02
- Core branch: `ai/chatgpt/company-report-multi-principal-core`
- Web branch: `ai/chatgpt/company-report-multi-principal-web`
- Production baselines: Core `5d03d93fe43670ec4136754050eab06e4dab2b0c`, Web
  `e210833f199cec5caafada7ad95f4e533c816c4c`, schema `20260902_0034`, policy generation 5

## Goal

Let the company-report BFF read all five explicitly authorized companies without broadening the
existing Web review identity. Browser requests must not choose or enlarge company scope.

## Frozen authorization boundary

- The existing Web certificate and principal remain unchanged except for the coordinated policy
  generation transition. Candidate review, evidence, personal finance, payroll, and commands keep
  using that primary client on `https://internal-ingress:8443`.
- A second certificate is bound to principal `workload:ledgerbridge-company-reports`, proxy SAN
  `spiffe://ledgerbridge.local/web/company-reports`, and Core ingress port `8444`.
- The report principal has exactly one capability: `company-report:read`. Only
  `/internal/v1/company-reports` and `/internal/v1/company-report-composition` accept it.
- The report principal has exactly five unique Entity grants. Every grant carries a complete
  immutable business-unit ref/UUID binding. It cannot carry account-registry authority.
- mTLS policy v1 remains accepted for a safe staged rollout and rollback. Policy v2 contains a bounded,
  unique set of certificate-serial, proxy-SAN, principal-ref bindings under one exact generation.
  Swapping a certificate between ports or reusing a serial, SAN, or principal fails closed.
- The Web BFF calls the unfiltered collection contract. Its public API rejects browser-supplied
  `company_ref`; the company selector only changes which already-authorized response item is
  rendered. Core may accept an internal `company_ref` only as a narrowing filter and still checks
  the verified principal grant.
- Candidate TEST, account-statement cash flow, and POSTED ledger facts remain separate. Pending or
  empty facts stay pending or zero according to the existing contracts; no amount is fabricated.

## Private policy preparation

The tracked builder never activates policy. Central integration supplies two private absolute
paths: the current v1 policy and a report-identity JSON containing the new certificate serial plus
the five reviewed grants. The input must not enter Git or logs.

```text
python scripts/build_company_report_mtls_policy.py \
  --current-policy <private-v1-policy> \
  --report-identity <private-five-company-identity-json> \
  --output <new-private-candidate-policy> \
  --expected-generation 5 \
  --target-generation 6
```

The default command is plan-only. Repeat with `--write` to create a new mode-0600 candidate file;
the builder refuses to replace an existing output. The private report identity shape is:

```json
{
  "certificate_serial": "<UPPERCASE_HEX_SERIAL>",
  "principal_ref": "workload:ledgerbridge-company-reports",
  "san_uri": "spiffe://ledgerbridge.local/web/company-reports",
  "grants": ["<exactly five private EntityGrant objects>"]
}
```

## Activation order owned by central integration

1. Create a revision-bound backup and pass isolated restore before changing code or policy.
2. Issue a CA-signed client-auth certificate with the report SAN; keep its key outside Git and
   mount it read-only into Web.
3. Run the builder in plan mode, then create and independently inspect the v2 candidate policy.
4. Stage the immutable Core and Web releases without switching the live containers. Do not run
   the new report-capability code against the v1 generation-5 policy: reports would correctly fail
   closed until the dedicated principal exists.
5. In one rollback-managed activation window, install the v2 policy, set Core and Web policy
   generation to 6, install the report client certificate/key, and switch to the reviewed Core and
   Web commits. Recreate the Core reader/ingress and Web only; other containers must keep identity
   and restart count.
6. Verify both exact certificate/port bindings, report-only capability, five-company collection,
   three isolated layers, two compositions, pending/empty states, and the public BFF's rejection
   of `company_ref` before declaring the rollout complete.

## Rollback

- A Web or policy failure rolls back Web code/config, the private policy file, and both generation
  values to generation 5, then recreates and health-checks reader/ingress and Web.
- If Core code must also roll back to `5d03d93`, restore the v1 policy first. The old Core cannot
  parse v2, so reversing that order would close every internal route.
- Restore the pre-change database backup only if an unexpected data mutation is detected; this
  read-only rollout is expected to leave every business table and audit count unchanged.
- Private certificate revocation/removal is separate from Git rollback and must be included if the
  report credential is suspected to be exposed.
