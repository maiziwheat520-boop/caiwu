"""ORM model registry.

Phase modules must be imported here so Alembic autogenerate sees every table.
"""

from ledgerbridge.models.evidence import (
    DispatchState,
    ImportDispatch,
    ImportJob,
    ImportJobStatus,
    IngestChannel,
    RawArtifact,
    SourceRecord,
    SourceSystem,
)
from ledgerbridge.models.ledger import (
    Account,
    AccountClass,
    AuditEvent,
    Entity,
    EntityType,
    JournalEntry,
    JournalStatus,
    Posting,
)

__all__ = [
    "Account",
    "AccountClass",
    "AuditEvent",
    "DispatchState",
    "Entity",
    "EntityType",
    "ImportDispatch",
    "ImportJob",
    "ImportJobStatus",
    "IngestChannel",
    "JournalEntry",
    "JournalStatus",
    "Posting",
    "RawArtifact",
    "SourceRecord",
    "SourceSystem",
]
