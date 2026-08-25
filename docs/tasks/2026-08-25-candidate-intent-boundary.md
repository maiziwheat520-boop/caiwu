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

The focused boundary regression in `tests/test_candidate_intent.py` covers the
financial handoff, entity/digest invariants, private-message admission, and
fail-closed triage outcomes. Ruff, strict mypy, compileall, and the focused
pytest run pass for this code-only slice. The protected Linux quality run for
the first implementation exposed the module as 0% covered (89.19% overall);
the follow-up commit adds this focused coverage without lowering the 90% gate.
