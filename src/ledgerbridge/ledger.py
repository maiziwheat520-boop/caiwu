"""Read-side helpers for posted ledger balances."""

from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ledgerbridge.audit import append_audit_event
from ledgerbridge.models.ledger import Account, AccountClass, JournalEntry, JournalStatus, Posting


def actual_account_balances(session: Session, entity_id: UUID) -> dict[UUID, int]:
    """Return account balances from POSTED entries only."""

    amount = case(
        (JournalEntry.status == JournalStatus.POSTED, Posting.amount_minor),
        else_=0,
    )
    rows = session.execute(
        select(Account.id, func.coalesce(func.sum(amount), 0))
        .outerjoin(Posting, Posting.account_id == Account.id)
        .outerjoin(JournalEntry, JournalEntry.id == Posting.entry_id)
        .where(Account.entity_id == entity_id)
        .group_by(Account.id)
    )
    return {account_id: int(balance) for account_id, balance in rows}


def actual_totals_by_class(session: Session, entity_id: UUID) -> dict[AccountClass, int]:
    """Return posted signed totals grouped by account class."""

    amount = case(
        (JournalEntry.status == JournalStatus.POSTED, Posting.amount_minor),
        else_=0,
    )
    rows = session.execute(
        select(Account.account_class, func.coalesce(func.sum(amount), 0))
        .outerjoin(Posting, Posting.account_id == Account.id)
        .outerjoin(JournalEntry, JournalEntry.id == Posting.entry_id)
        .where(Account.entity_id == entity_id)
        .group_by(Account.account_class)
    )
    totals = {account_class: 0 for account_class in AccountClass}
    totals.update({account_class: int(total) for account_class, total in rows})
    return totals


def post_journal_entry(
    session: Session,
    entry_id: UUID,
    *,
    actor: str,
    reason: str,
    rule_version: str | None = None,
) -> UUID:
    """Bind a fresh journal.post event and transition one DRAFT in the caller's transaction."""

    entry = session.scalar(
        select(JournalEntry).where(JournalEntry.id == entry_id).with_for_update()
    )
    if entry is None:
        raise LookupError("journal entry does not exist")
    if entry.status is not JournalStatus.DRAFT:
        raise ValueError("only DRAFT journal entries can be posted")
    audit_event_id = append_audit_event(
        session,
        actor=actor,
        action="journal.post",
        reason=reason,
        rule_version=rule_version,
        payload={"journal_entry_id": str(entry.id)},
    )
    entry.posted_audit_event_id = audit_event_id
    entry.status = JournalStatus.POSTED
    session.flush()
    return audit_event_id
