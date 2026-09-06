# Candidate intent and evidence-preservation boundary (2026-08-25)

`ledgerbridge.candidate_intent` is the handoff from an admitted, financially
triaged Hermes message into the Core candidate workflow. It requires an
explicit `CANDIDATE` triage action, a source message/event identity, an entity,
and at least one immutable evidence binding. Every evidence binding must use
the same entity and a 32-byte digest; mismatches fail closed.

This boundary creates no database row, Candidate revision, JournalEntry,
Posting, or automatic decision. The next persistence layer must append the
candidate creation audit event and preserve the existing Candidate state
machine before human review.

## Manual replay

```text
uv run --frozen --extra dev python scripts/r1_synthetic_candidate_intent_demo.py
{"candidate_ref": "30000000-0000-4000-8000-000000000010", "entity_ref": "10000000-0000-4000-8000-000000000001", "evidence_count": 1, "mode": "synthetic", "source_message_id": "msg-candidate", "writes_posting": false}
```

Ruff, strict mypy, compileall, and diff-check pass for this code-only slice.
