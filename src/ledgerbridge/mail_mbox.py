"""Read-only bounded mbox adapter for OAuth-authenticated desktop clients."""

from __future__ import annotations

import mailbox
from collections.abc import Iterator
from pathlib import Path

from ledgerbridge.mail_collector import MailCollectorError, MailMessage
from ledgerbridge.mail_imap import MAX_IMAP_MESSAGE_BYTES, parse_imap_message


class MboxMailProvider:
    """Read messages already synchronized by a desktop mail client.

    Thunderbird performs the OAuth login; this adapter never sees the token.
    The mbox file is opened read-only and only the newest bounded messages are
    projected into the common mail provider contract.
    """

    def __init__(self, path: Path, *, max_messages: int = 5) -> None:
        resolved = path.resolve()
        if not resolved.is_file():
            raise ValueError("mbox path must be an existing file")
        if max_messages <= 0 or max_messages > 20:
            raise ValueError("max_messages is out of bounds")
        self._path = resolved
        self._max_messages = max_messages

    def iter_messages(self) -> Iterator[MailMessage]:
        try:
            box = mailbox.mbox(self._path, create=False)
            try:
                keys = list(box.iterkeys())
                for key in reversed(keys[-self._max_messages :]):
                    raw = box.get_bytes(key)
                    if len(raw) > MAX_IMAP_MESSAGE_BYTES:
                        raise MailCollectorError(
                            "MAIL_MESSAGE_TOO_LARGE", "mbox message exceeds staging limit"
                        )
                    yield parse_imap_message(raw, fallback_id=str(key))
            finally:
                box.close()
        except MailCollectorError:
            raise
        except (OSError, mailbox.Error, ValueError) as exc:
            raise MailCollectorError("MAIL_PROVIDER_UNAVAILABLE", "mbox read failed") from exc
