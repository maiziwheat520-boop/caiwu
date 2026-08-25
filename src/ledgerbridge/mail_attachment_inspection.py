"""In-memory attachment format inspection for the staging review boundary."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from ledgerbridge.mail_collector import MailAttachment


@dataclass(frozen=True, slots=True)
class AttachmentInspection:
    filename: str
    media_type: str
    format: str
    size_bytes: int
    encrypted: bool | None
    requires_review: bool


def inspect_attachment(attachment: MailAttachment) -> AttachmentInspection:
    suffix = PurePosixPath(attachment.filename).suffix.casefold()
    detected_format = {
        ".zip": "zip",
        ".pdf": "pdf",
        ".ofd": "ofd",
        ".xml": "xml",
    }.get(suffix, attachment.media_type.casefold() or "unknown")
    encrypted: bool | None = None
    requires_review = True
    if suffix == ".zip":
        encrypted = _zip_is_encrypted(attachment.content)
    return AttachmentInspection(
        filename=attachment.filename,
        media_type=attachment.media_type,
        format=detected_format,
        size_bytes=len(attachment.content),
        encrypted=encrypted,
        requires_review=requires_review,
    )


def _zip_is_encrypted(content: bytes) -> bool | None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
    except (OSError, ValueError, zipfile.BadZipFile):
        return None
    return bool(entries) and any(entry.flag_bits & 0x1 for entry in entries)
