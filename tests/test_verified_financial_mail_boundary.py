from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from ledgerbridge.mail_collector import MailAttachment, MailMessage
from scripts import r1_parse_verified_financial_mail as parse_mail
from scripts import r1_staging_financial_zip_check as staging_check


def _message(*, duplicate_attachment: bool = False) -> MailMessage:
    attachment = MailAttachment(
        attachment_id="attachment-1",
        filename="statement.pdf",
        media_type="application/pdf",
        content=b"encrypted-pdf-fixture",
    )
    attachments = (attachment, attachment) if duplicate_attachment else (attachment,)
    return MailMessage(
        message_id="message-1",
        subject="中国银行交易流水",
        received_at="2026-08-26T08:00:00+00:00",
        attachments=attachments,
        sender_address="trusted@example.test",
        resent_from_address="forwarder@example.test",
    )


def _manifest_record(message: MailMessage, **overrides: object) -> dict[str, object]:
    attachment = message.attachments[0]
    record: dict[str, object] = {
        "message_id": message.message_id,
        "subject": message.subject,
        "sender": message.sender_address,
        "forwarder": message.resent_from_address,
        "received_at": message.received_at,
        "filename": attachment.filename,
        "attachment_sha256": hashlib.sha256(attachment.content).hexdigest(),
        "size_bytes": len(attachment.content),
        "media_type": attachment.media_type,
        "document_type": "BOC_PDF",
        "encrypted": True,
        "verified_at": "2026-08-26T08:01:00+00:00",
    }
    record.update(overrides)
    return record


def _configure_parse_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    message: MailMessage,
    *,
    record: dict[str, object] | None,
) -> None:
    password_path = tmp_path / "passwords.json"
    password_path.write_text(
        json.dumps({"schema_version": 1, "entries": []}),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.json"
    if record is not None:
        manifest_path.write_text(
            json.dumps({"schema_version": 1, "records": [record]}),
            encoding="utf-8",
        )
    monkeypatch.setattr(parse_mail, "DEFAULT_ONE_TIME_PASSWORDS", password_path)
    monkeypatch.setattr(parse_mail, "DEFAULT_VERIFICATION_MANIFEST", manifest_path)
    monkeypatch.setattr(parse_mail, "_load_messages", lambda: (message,))


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("missing_manifest", "verification manifest is unavailable"),
        ("ambiguous_attachment", "verified attachment is missing or ambiguous"),
        ("metadata_changed", "verified mail metadata changed"),
    ],
)
def test_parse_verified_mail_fails_closed_on_unbound_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
    expected_error: str,
) -> None:
    message = _message(duplicate_attachment=case == "ambiguous_attachment")
    record = None if case == "missing_manifest" else _manifest_record(message)
    if case == "metadata_changed":
        assert record is not None
        record["sender"] = "changed@example.test"
    _configure_parse_inputs(monkeypatch, tmp_path, message, record=record)

    with pytest.raises(RuntimeError, match=expected_error):
        parse_mail.parse_verified_mail()


def test_parse_verified_mail_exactly_consumes_bound_attachment_and_preserves_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    message = _message()
    record = _manifest_record(message)
    digest = str(record["attachment_sha256"])
    password_path = tmp_path / "passwords.json"
    password_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "status": "verified",
                        "matched_subject": message.subject,
                        "attachment_filename": message.attachments[0].filename,
                        "message_id": message.message_id,
                        "attachment_sha256": digest,
                        "password": "test-only-password",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"schema_version": 1, "records": [record]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(parse_mail, "DEFAULT_ONE_TIME_PASSWORDS", password_path)
    monkeypatch.setattr(parse_mail, "DEFAULT_VERIFICATION_MANIFEST", manifest_path)
    monkeypatch.setattr(parse_mail, "_load_messages", lambda: (message,))
    monkeypatch.setattr(
        parse_mail,
        "parse_boc_pdf",
        lambda *_args, **_kwargs: SimpleNamespace(
            to_dict=lambda: {"source": "statement.pdf", "bank_code": "BOC"}
        ),
    )

    payload = parse_mail.parse_verified_mail()

    statements = payload["statements"]
    assert isinstance(statements, list)
    assert len(statements) == 1
    statement = statements[0]
    assert statement["source"] == "statement.pdf"
    assert statement["source_mail"] == {
        "message_id": message.message_id,
        "subject": message.subject,
        "received_at": message.received_at,
        "sender": message.sender_address,
        "forwarder": message.resent_from_address,
        "attachment_filename": message.attachments[0].filename,
        "attachment_sha256": digest,
    }
    assert statement["admission"] == {
        "verification": "password_decryption",
        "mail_authentication": "not_verified",
        "identity_binding": "message_id_and_attachment_sha256",
        "metadata_binding": "document_content",
        "text_fields": "layout_derived_requires_review",
        "requires_review": True,
    }


def test_atomic_output_is_complete_and_no_clobber_by_default(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    first: dict[str, object] = {"schema_version": 1, "statements": [{"bank_code": "BOC"}]}
    parse_mail._atomic_write_json(output, first, replace=False)

    assert json.loads(output.read_text(encoding="utf-8")) == first
    original_bytes = output.read_bytes()
    with pytest.raises(RuntimeError, match="output already exists"):
        parse_mail._atomic_write_json(output, {"schema_version": 2}, replace=False)
    assert output.read_bytes() == original_bytes


def test_replace_writer_uses_shared_lock_and_no_clobber_cannot_overwrite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "result.json"
    lock_path = output.with_name(f".{output.name}.lock")
    replacement: dict[str, object] = {"schema_version": 2, "writer": "replace"}
    original_lock = parse_mail._exclusive_file_lock
    attempted_lock = threading.Event()
    failures: list[BaseException] = []

    @contextmanager
    def observed_lock(path: Path) -> Iterator[None]:
        attempted_lock.set()
        with original_lock(path):
            yield

    def write_replacement() -> None:
        try:
            parse_mail._atomic_write_json(output, replacement, replace=True)
        except BaseException as exc:  # pragma: no cover - asserted after joining
            failures.append(exc)

    monkeypatch.setattr(parse_mail, "_exclusive_file_lock", observed_lock)
    writer = threading.Thread(target=write_replacement, daemon=True)
    with original_lock(lock_path):
        writer.start()
        assert attempted_lock.wait(timeout=2)
        writer.join(timeout=0.05)
        assert writer.is_alive()
        assert not output.exists()
    writer.join(timeout=2)

    assert not writer.is_alive()
    assert failures == []
    assert json.loads(output.read_text(encoding="utf-8")) == replacement
    published_bytes = output.read_bytes()
    with pytest.raises(RuntimeError, match="output already exists"):
        parse_mail._atomic_write_json(output, {"schema_version": 3}, replace=False)
    assert output.read_bytes() == published_bytes


def test_verification_manifest_rejects_duplicate_identity_keys(tmp_path: Path) -> None:
    message = _message()
    record = _manifest_record(message)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"schema_version": 1, "records": [record, record]}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="verification manifest contains a duplicate"):
        staging_check._record_verifications(manifest_path, message, [])
