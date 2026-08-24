# Authentication and Connector boundary design context

This is a derived design context, not source evidence. The source snapshot is
the Codex branch `ai/chatgpt/phase-3-connector-runner` at revision
`48c695709c60db578242d290e7e1d7b5881150df`, with no working-tree drift when
the collection was prepared.

The evidence collection is the following twelve repository files. The
collection digest is the SHA-256 of the newline-delimited `path|file-sha256`
manifest (paths are repository-relative):

`79175486a1716efbf17828e49efa809caeae5b74ba0b48d2ca0f2f80e6ee0149`

| Evidence | Source | SHA-256 | What we inspected |
| --- | --- | --- | --- |
| A01 | `src/ledgerbridge/main.py` | `56f733b6d415cfc8bd4b68f0bffa3f7e77bdb2de6c2f872a19ab79b0b680ea0c` | Feature flag, trusted-principal seam, empty connector dependency, route ordering and bounded response. |
| A02 | `docs/reviews/2026-08-23-internal-upload-route-implementation-codex.md` | `d9e71c4801f6878a6628c74f5ec84110e50324e41930c1821c3582cea4fab0bf` | Implemented route acceptance evidence and residual gates. |
| A03 | `src/ledgerbridge/connectors.py` | `d441ac4ecb40d5298788a60a6385c3eedc914e6a3716043395f246ed010719f8` | Connector identity, execution-mode and record-contract validation. |
| A04 | `src/ledgerbridge/imports.py` | `c2176a58da5362306e555be2f887e84b4ca891172c17114610fa9e66df8b64bd` | Connector-set validation, detection, source-system binding and importer-owned terminal state. |
| A05 | `src/ledgerbridge/connector_runner.py` | `fa3e17eecd3e3f727f41ada1eee8660e8969f760e498cb5aa365a6b4fdd88920` | Empty runner registry, request identity checks, bounded execution and no-network supervisor boundary. |
| A06 | `src/ledgerbridge/runner_client.py` | `1ba53c6f6cb368ca9506cf7f00996bd5804a50c27205a47082c08807ed3c3814` | Runner facade and Unix-socket client contract. |
| A07 | `src/ledgerbridge/worker.py` | `bc26f9a8c0f25179396bb001f3bae3236e9f3d6daee762cb39b7cf1abfcd4e4b` | Current worker composition root, which builds the importer but no connector registry. |
| A08 | `docker-compose.yml` | `0f284a68a7f63051d191c062dc6fd468db7e56e1e2003013f3122797cb754b6d` | API/worker/runner mounts, networks, profiles and production-default flag. |
| A09 | `docker/app.Dockerfile` | `adaab609ffb336b724f0f567442918a8bea70dc35c0247f6cec5fb28ed04d6f3` | API image privileges and artifact mount assumptions. |
| A10 | `docker/connector-runner.Dockerfile` | `627bda9a94c5358624218da6ee15a3ecdd213cbc467584d4621117c5eea5ec76` | Runner image identity, read-only filesystem, dropped capabilities and UID. |
| A11 | `docs/tasks/2026-08-23-phase-3-slice-c-upload-endpoint-design.md` | `ba4c820a54a8474d86628a7a8803617a0673c8d3c386de41bc6da04972be4d8e` | Approved internal route contract and explicit authentication/manifest gates. |
| A12 | `src/ledgerbridge/config.py` | `d9ffaa86e5c90e0d59e7d2df9fc36a111c7a8180214bac423869cbc74ab60062` | Environment configuration; no provider or manifest settings exist yet. |

## Observed facts

- The route accepts a principal only from `request.state.authenticated_principal`.
  There is no authentication middleware, issuer/audience policy, token verifier,
  scope check, or principal freshness check in the snapshot (A01, A02).
- The route is default-disabled and production-forced-off. This is a deliberate
  safety gate, not evidence that the missing provider has been solved (A01, A02,
  A08).
- `get_internal_connectors()` returns an empty sequence. `ConnectorSupervisor`
  also starts with an empty registry unless tests inject one (A01, A05).
- Connector identities are validated for name, version, source system,
  execution mode, uniqueness and record provenance. Production connectors must
  select `execution_mode=runner`, but no manifest loader enforces a complete
  deployment-wide set or pins implementation factories (A03, A04, A05).
- API and worker do not currently mount `connector-socket`; only the worker
  mounts it, while the runner is an optional no-network profile. A synchronous
  API route therefore cannot use a production `RunnerConnector` without a
  separate composition decision (A06, A07, A08).
- The database has append-only `ingest_channel` and `source_system` registries,
  but the manifest and auth provider are not represented there. Registration and
  deployment ownership must remain separate from request-controlled values (A04,
  A11).

## Inferences and limitations

We infer that the two missing controls share one admission problem: the route
needs a verifiable actor and a reviewable, immutable set of connector
capabilities before it can safely leave its default-off state. The source does
not establish a preferred identity provider, key-management service, signed
manifest format, reload SLA, or production workload latency budget. Those are
design choices below, not measured facts.

This collection does not include a public exposure, real credential, real
Connector, or production request trace. The proposals consequently describe
pre-enable controls and validation work; they do not claim that authentication
or connector registration is implemented.
