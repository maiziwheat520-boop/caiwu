---
status: accepted
---

# Isolate password-protected evidence unlock in a dedicated sidecar

LedgerBridge will unlock reviewed password-protected evidence through a dedicated
`evidence-unlocker` process connected to the Core API by a private Unix domain socket. The API
keeps its evidence volume read-only. The sidecar receives only a request-bound source descriptor,
operation identity, nonce, and the one-request password; it has the evidence key and a writable
encrypted-artifact volume, but no database credential or network.

The browser password travels only in the authenticated HTTP request and the bounded in-memory
socket frame. It is never accepted through a command-line argument or environment variable and is
not written to the database, broker, artifact metadata, audit event, or application log. Python
does not guarantee complete zeroization of immutable strings, so the security property is bounded
lifetime and no intentional persistence or echo, not a claim of perfect process-memory erasure.

Core authorizes the reviewed source and persists a non-secret operation reservation before calling
the sidecar. The sidecar authenticates and decrypts the approved encrypted source, validates ZIP
member count, names, encryption, compression ratio, and output size, and writes only encrypted
outputs. Core then atomically records their Evidence Objects, the successful output fact, receipt,
and audit event before returning `UNLOCKED`. A failed database completion can leave unreachable
encrypted ciphertext; it is not projected as evidence and is eligible for bounded orphan cleanup.

## Consequences

- The sidecar and API must run as the same dedicated UID. The socket directory is mode `0700`, the
  socket is mode `0600`, the API mount is read-only, and both peers verify local identity.
- Operation id and assertion nonce are one-to-one. Exact successful replay returns the same output
  facts; rebinding, malformed frames, stale identities, and oversized requests fail closed.
- Production compose keeps the route disabled and the sidecar behind an explicit profile. Enabling
  the route additionally requires the database backend, U1 operational gate, absolute socket path,
  reviewed source facts, and the reserved schema migration.
- A generic queued worker was rejected because it would put the password into broker or job state.
  Making the API artifact mount writable was rejected because it broadens the public API process's
  authority. In-process extraction was rejected because parser risk would share the API boundary.
- The initial processor supports password-encrypted ZIP members only. Unsupported encryption or
  archive formats return the same fixed rejection response and do not weaken parser or filesystem
  policy.
