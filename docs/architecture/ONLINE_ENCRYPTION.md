# Online encryption foundation

S1 adds a deployment-neutral encryption foundation for synthetic data.  It does
not assert that the current Hermes host, PostgreSQL volume, or production keys
are protected.

## Application format

`ledgerbridge.secretstream.v1` uses libsodium
XChaCha20-Poly1305 secretstream with these invariants:

- every object receives a new random 256-bit data-encryption key (DEK);
- a caller-injected `KeyProvider` wraps the DEK under an external key generation;
- the canonical header records only the algorithm, format, chunk size, public
  secretstream header, opaque wrapped DEK, nonce, and non-secret generation ID;
- purpose, caller AAD, immutable header fields, and the monotonically increasing
  frame index are authenticated but not stored as untrusted authority;
- every stream has an authenticated `FINAL` frame; truncation, extra frames,
  reordering, bad tags, wrong purpose/AAD, missing generation, and unknown format
  fail closed;
- KEK rotation can rewrap only the DEK. New writes use the active generation;
  readers may retain explicitly configured old generations during a bounded
  transition.

`SyntheticKeyProvider` is test-only and has no file, environment, command, or
network loading path. A future deployment adapter must keep KEK material outside
the repository, database, `.env`, Compose build context, logs, and ciphertext
volume.

## Encrypted persistence primitives

- `EncryptedArtifactStore` encrypts before delegating to the existing durable
  ArtifactStore. Its durable staging and published paths contain ciphertext;
  the storage key is the ciphertext digest rather than the plaintext digest.
  Opening first authenticates the complete envelope, then verifies the expected
  plaintext size and SHA-256.
- `EncryptedSpool` protects API multipart and isolated-runner spill files with a
  per-instance in-memory key. Small caller writes are coalesced into bounded
  frames. It authenticates the complete stream before the caller can read any
  plaintext and permanently closes itself if sealing fails. A crash-left spool
  is intentionally unrecoverable ciphertext, not retry state.
- `EncryptedStateStore` provides purpose-separated outbox, retry, OAuth token-map,
  and identity-map primitives with random opaque handles, per-write DEKs,
  generation CAS, TTL, atomic replacement, and authenticated logical revocation.
  Revocation does not claim physical erasure from snapshots, backups, journals,
  caches, or media.

These primitives use only synthetic keys in S1. Hermes/Graph adapters and real
state are out of scope until their later gates.

## Host attestation contract

`scripts/storage_encryption_preflight.py` parses a short-lived, host/boot/revision
bound snapshot and defaults to `FAIL`. It requires the ArtifactStore, PGDATA,
`pg_wal`, PostgreSQL temp directories, and every tablespace to resolve through
approved active LUKS/dm-crypt mappings. Swap must be absent or separately
approved and encrypted; core dumps must be disabled; key custody must not share
the data, deployment, or backup filesystem identity. The verdict contains only
checks, reason codes, and expected bindings, never the submitted evidence or key
material.

The parser does not collect or sign host evidence. Production collection needs a
separately reviewed privileged deployment script and an independently protected
attestation channel.

## Explicitly incomplete

- Hermes currently stores Docker volumes on the unencrypted ext4 system disk.
- No production KeyProvider/KMS/Vault/systemd-credential adapter exists.
- The legacy `ArtifactStore` and existing database schema remain for historical
  synthetic flows; real mode must use encrypted artifacts and replace raw
  filename/locator/arbitrary payload exposure before I1.
- `EncryptedStateStore` authenticates the contents of each state object but has
  no external monotonic generation/tombstone anchor. Restoring an older valid
  ciphertext can therefore roll back expiry or revocation. Its portable
  synthetic lock also does not qualify a production state directory's owner,
  mode, inode-stable lock, or stale-lock recovery. Real state requires a
  separately reviewed DB/KMS/append-only anti-rollback anchor and hardened host
  lock/permission adapter.
- DEK `rewrap` validates envelope framing but deliberately does not decrypt or
  authenticate payload frames. A production rotation workflow must pair rewrap
  with a prior or subsequent complete authenticated read/receipt and must never
  treat rewrap success as a data-health proof.
- The existing encrypted GPG backup/restore workflow has not yet been taught to
  authenticate encrypted-artifact plaintext metadata or require an encrypted
  restore target.
- PostgreSQL, WAL, temporary space, crash dumps, host swap policy, key recovery,
  and fresh-host restore have not passed an operational rehearsal.

`LEDGERBRIDGE_ENABLE_REAL_INGEST=true` is therefore rejected unconditionally.
R1 may consume these primitives with empty/synthetic data, but no real source may
be enabled until the remaining S1 operational evidence and the later I1/D1 gates
pass.
