"""The local backup must produce a proof, not a file that looks like one.

Core refuses a statement import without an encrypted backup and a passed
isolated restore whose inventory equals the live counts. Local mode satisfies
that by actually taking the backup and actually rehearsing the restore. The
risk this file guards is the opposite temptation: a helper that writes
plausible JSON and calls it evidence.

The parts that need Docker, PostgreSQL and GnuPG are exercised by running the
thing; what is tested here is the reasoning around them - that the archive is
reproducible, that a mangled tool output is reported as what went wrong rather
than as the last line printed, and that the inventory shape the gate reads is
the shape this module writes.
"""

from __future__ import annotations

import io
import json
import tarfile
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from ledgerbridge.local_backup import (
    BACKUP_FORMAT,
    CIPHERTEXT_NAME,
    REHEARSAL_FORMAT,
    LocalBackupError,
    LocalDatabase,
    _build_archive,
    _diagnosis,
    _first_fingerprint,
    _gnupg_home_argument,
    _member,
    existing_backups,
)
from ledgerbridge.mybank_statement_cutover import (
    _SUPPORTED_SCHEMA_REVISIONS,
    MyBankCutoverSafetyProof,
    MyBankStatementCutoverError,
    MyBankStatementCutoverGates,
    production_counts_from_cutover_inventory,
    verify_mybank_cutover_safety_proof,
)

INVENTORY = {
    "schema_revision": next(iter(sorted(_SUPPORTED_SCHEMA_REVISIONS))),
    "candidate_total": 2,
    "latest_pending_candidates": 1,
    "audit_events": 7,
    "row_counts": {
        # The sixteen tables the gate reads by name.
        "evidence_object": 1,
        "encrypted_object_identity": 1,
        "encrypted_blob_version": 1,
        "managed_account": 1,
        "managed_account_lifecycle": 1,
        "account_registry_operation": 1,
        "managed_account_alias": 1,
        "account_business_unit_assignment": 1,
        "fact_business_unit_allocation_set": 0,
        "fact_business_unit_allocation_item": 0,
        "bank_statement": 1,
        "bank_statement_transaction": 3,
        "bank_statement_observation": 3,
        "bank_statement_review": 1,
        "journal_entry": 0,
        "posting": 0,
        # And the ones it does not, which a local backup counts anyway.
        "entity": 1,
        "business_unit": 1,
        "audit_event": 7,
    },
}


def test_the_archive_is_byte_identical_for_an_unchanged_database(tmp_path: Path) -> None:
    """Twice over the same inputs must give the same bytes.

    This is what lets "the ciphertext digest changed" mean "the ledger
    changed". If tar recorded the wall clock, every backup would differ from
    every other one and the digest would say nothing at all.
    """

    artifacts = tmp_path / "artifacts"
    (artifacts / "blobs").mkdir(parents=True)
    (artifacts / "blobs" / "object.bin").write_bytes(b"ciphertext")

    first = _build_archive(dump=b"PGDMP", inventory=INVENTORY, artifact_root=artifacts)
    second = _build_archive(dump=b"PGDMP", inventory=INVENTORY, artifact_root=artifacts)

    assert first == second


def test_the_archive_carries_the_dump_the_inventory_and_the_artifacts(tmp_path: Path) -> None:
    """A dump alone would restore a ledger whose evidence blobs are gone."""

    artifacts = tmp_path / "artifacts"
    (artifacts / "blobs").mkdir(parents=True)
    (artifacts / "blobs" / "object.bin").write_bytes(b"ciphertext")

    archive = _build_archive(dump=b"PGDMP", inventory=INVENTORY, artifact_root=artifacts)

    assert _member(archive, "database.dump") == b"PGDMP"
    assert json.loads(_member(archive, "inventory.json")) == INVENTORY
    assert _member(archive, "artifacts/blobs/object.bin") == b"ciphertext"


def test_the_archive_records_no_timestamp_or_owner(tmp_path: Path) -> None:
    archive = _build_archive(dump=b"PGDMP", inventory=INVENTORY, artifact_root=tmp_path / "none")

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as opened:
        members = opened.getmembers()
    assert members
    for member in members:
        assert member.mtime == 0
        assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, "", "")


def test_a_symlink_in_the_artifact_store_is_refused(tmp_path: Path) -> None:
    """Following one would back up whatever it points at, under its name."""

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "real.bin").write_bytes(b"ciphertext")
    try:
        (artifacts / "link.bin").symlink_to(artifacts / "real.bin")
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged Windows
        pytest.skip("this machine does not allow creating symlinks")

    with pytest.raises(LocalBackupError, match="symlink"):
        _build_archive(dump=b"PGDMP", inventory=INVENTORY, artifact_root=artifacts)


def test_a_missing_archive_member_is_named(tmp_path: Path) -> None:
    archive = _build_archive(dump=b"PGDMP", inventory=INVENTORY, artifact_root=tmp_path / "none")

    with pytest.raises(LocalBackupError, match="missing artifacts/absent"):
        _member(archive, "artifacts/absent")


def test_the_inventory_converts_to_the_counts_the_gate_compares() -> None:
    """The gate reads sixteen named tables and three scalars out of this.

    Counting every base table rather than those sixteen is deliberate: the
    extra keys are ignored by the gate and are stronger evidence that the
    restored database equals the live one, not weaker.
    """

    counts = production_counts_from_cutover_inventory(
        INVENTORY,
        expected_schema_revision=INVENTORY["schema_revision"],  # type: ignore[arg-type]
    )

    assert counts.bank_statement_transactions == 3
    assert counts.bank_statement_observations == 3
    assert counts.audit_events == 7
    assert counts.candidates == 2
    assert counts.latest_pending_candidates == 1


def test_the_diagnosis_is_the_line_that_says_what_went_wrong() -> None:
    """psql ends its complaints with a caret pointing at the offending column.

    Reporting the last line therefore reports a lone "^", which is how a
    failing backup came to say nothing at all about why it failed.
    """

    stderr = b'ERROR:  column "nope" does not exist\nLINE 1: SELECT nope\n               ^\n'

    assert _diagnosis(stderr) == 'ERROR:  column "nope" does not exist'


def test_the_diagnosis_falls_back_to_whatever_was_printed() -> None:
    assert _diagnosis(b"could not connect to server\n") == "could not connect to server"
    assert _diagnosis(b"") == "no output"
    assert _diagnosis(b"^\n") == "no output"


def test_a_fingerprint_is_read_from_the_colon_listing() -> None:
    listing = (
        "sec:u:3072:1:0123456789ABCDEF:::::::\n"
        "fpr:::::::::0123456789ABCDEF0123456789ABCDEF01234567:\n"
    )

    assert _first_fingerprint(listing) == "0123456789ABCDEF0123456789ABCDEF01234567"


def test_a_listing_without_a_usable_fingerprint_yields_nothing() -> None:
    assert _first_fingerprint("uid:u::::::::Local Backup:\n") is None
    assert _first_fingerprint("fpr:::::::::not-a-fingerprint:\n") is None


def test_the_gnupg_home_is_spelled_the_way_this_gpg_spells_its_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Git for Windows ships an MSYS gpg that wants /c/Users/..., not C:\\Users.

    Handed a drive-letter path it reads it as relative and reports a keyring it
    cannot find, which looks like a missing key rather than a spelling
    disagreement. So ask gpg how it writes its own Home and follow suit.
    """

    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setattr(
        "ledgerbridge.local_backup._run",
        lambda *args, **kwargs: b"gpg (GnuPG) 2.4.5\nHome: /c/Users/someone/.gnupg\n",
    )

    spelled = _gnupg_home_argument(tmp_path)

    assert spelled.startswith("/")
    assert ":" not in spelled


def test_a_drive_letter_gpg_keeps_the_drive_letter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setattr(
        "ledgerbridge.local_backup._run",
        lambda *args, **kwargs: b"gpg (GnuPG) 2.4.7\nHome: C:\\Users\\someone\\AppData\n",
    )

    assert _gnupg_home_argument(tmp_path) == str(tmp_path)


def test_a_hand_written_proof_is_still_refused(tmp_path: Path) -> None:
    """The point of the module is that it does not need to forge this.

    Writing the sidecar and the rehearsal by hand, with no ciphertext behind
    them, must fail verification - otherwise producing the artifacts locally
    would be indistinguishable from producing them dishonestly.
    """

    directory = tmp_path / "local-backup-20260912T000000Z"
    directory.mkdir()
    (directory / "backup.json").write_text(
        json.dumps(
            {
                "format": BACKUP_FORMAT,
                "created_at": "2026-09-12T00:00:00+00:00",
                "revision": "0" * 40,
                "gpg_fingerprint": "0" * 40,
                "ciphertext": CIPHERTEXT_NAME,
                "ciphertext_sha256": "0" * 64,
                "postgres_image": "postgres:16",
            }
        ),
        encoding="utf-8",
    )
    report = tmp_path / "restore-rehearsal-20260912T000000Z.json"
    report.write_text(
        json.dumps({"format": REHEARSAL_FORMAT, "status": "passed"}), encoding="utf-8"
    )

    with pytest.raises(MyBankStatementCutoverError, match="proof is invalid"):
        verify_mybank_cutover_safety_proof(
            MyBankCutoverSafetyProof(backup_directory=directory, restore_report=report),
            gates=MyBankStatementCutoverGates(
                schema_revision=INVENTORY["schema_revision"],  # type: ignore[arg-type]
                backup_verified=True,
                isolated_restore_verified=True,
                rollback_ready=True,
                expected_before=production_counts_from_cutover_inventory(
                    INVENTORY,
                    expected_schema_revision=INVENTORY["schema_revision"],  # type: ignore[arg-type]
                ),
            ),
        )


def test_a_local_database_is_the_four_things_the_backup_needs() -> None:
    database = LocalDatabase(
        container="ledgerbridge-local-postgres",
        database="ledgerbridge",
        owner_role="ledgerbridge_owner",
        image="postgres:16",
    )

    assert database.container and database.database
    assert database.owner_role.endswith("_owner")
    with pytest.raises(FrozenInstanceError):
        database.database = "other"  # type: ignore[misc]


def test_existing_backups_are_newest_first_and_carry_their_inventory(tmp_path: Path) -> None:
    for stamp, transactions in (("20260912T000000Z", 2), ("20260912T010000Z", 5)):
        directory = tmp_path / f"local-backup-{stamp}"
        directory.mkdir()
        (directory / "backup.json").write_text(json.dumps({"revision": "a" * 40}), encoding="utf-8")
        inventory = dict(INVENTORY)
        inventory["row_counts"] = dict(INVENTORY["row_counts"])  # type: ignore[arg-type]
        inventory["row_counts"]["bank_statement_transaction"] = transactions  # type: ignore[index]
        (directory / f"restore-rehearsal-{stamp}.json").write_text(
            json.dumps(
                {
                    "format": REHEARSAL_FORMAT,
                    "status": "passed",
                    "isolated_database": f"rehearsal_{stamp}",
                    "source_database_metadata": {"cutover_inventory": inventory},
                }
            ),
            encoding="utf-8",
        )

    found = existing_backups(tmp_path)

    assert [backup.directory.name for backup in found] == [
        "local-backup-20260912T010000Z",
        "local-backup-20260912T000000Z",
    ]
    assert found[0].inventory["row_counts"]["bank_statement_transaction"] == 5
    assert found[0].revision == "a" * 40


def test_a_half_written_backup_is_not_offered(tmp_path: Path) -> None:
    """A directory with no passed rehearsal is not a proof of anything."""

    no_report = tmp_path / "local-backup-20260912T000000Z"
    no_report.mkdir()
    (no_report / "backup.json").write_text("{}", encoding="utf-8")

    failed = tmp_path / "local-backup-20260912T010000Z"
    failed.mkdir()
    (failed / "backup.json").write_text("{}", encoding="utf-8")
    (failed / "restore-rehearsal-20260912T010000Z.json").write_text(
        json.dumps({"format": REHEARSAL_FORMAT, "status": "failed"}), encoding="utf-8"
    )

    assert existing_backups(tmp_path) == ()


def test_no_backup_directory_at_all_is_not_an_error(tmp_path: Path) -> None:
    assert existing_backups(tmp_path / "absent") == ()
