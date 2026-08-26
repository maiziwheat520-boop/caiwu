from __future__ import annotations

import base64
from collections.abc import Mapping

import pytest

from ledgerbridge.mail_collector import (
    GRAPH_HOST,
    MailCollector,
    MailCollectorError,
    MicrosoftGraphMailProvider,
)


class StaticToken:
    def get_access_token(self) -> str:
        return "x" * 32


class MappingTransport:
    def __init__(self, responses: Mapping[str, Mapping[str, object]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def get_json(self, path: str, *, authorization: str) -> Mapping[str, object]:
        self.calls.append((path, authorization))
        return self.responses[path]


def _provider(transport: MappingTransport) -> MicrosoftGraphMailProvider:
    return MicrosoftGraphMailProvider(
        transport,
        StaticToken(),
        mailbox="ops@example.test",
    )


def test_disabled_collector_is_fail_closed() -> None:
    with pytest.raises(MailCollectorError, match="disabled") as caught:
        next(MailCollector().collect())
    assert caught.value.error_code == "MAIL_PROVIDER_DISABLED"


def test_graph_provider_streams_bounded_attachment_and_quotes_ids() -> None:
    message_path = (
        "/v1.0/users/ops%40example.test/mailFolders/inbox/messages?%24top=20&"
        "%24select=id%2Csubject%2CreceivedDateTime%2ChasAttachments%2CbodyPreview"
    )
    attachment_path = "/v1.0/users/ops%40example.test/messages/msg%2F1/attachments?%24top=32"
    transport = MappingTransport(
        {
            message_path: {
                "value": [
                    {
                        "id": "msg/1",
                        "subject": "Invoice",
                        "receivedDateTime": "2026-08-24T00:00:00Z",
                        "hasAttachments": True,
                    }
                ]
            },
            attachment_path: {
                "value": [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "id": "att-1",
                        "name": "invoice.csv",
                        "contentType": "text/csv",
                        "contentBytes": base64.b64encode(b"csv").decode(),
                        "size": 3,
                    }
                ]
            },
        }
    )

    collected = tuple(MailCollector(_provider(transport)).collect())

    assert collected[0].message_id == "msg/1"
    assert collected[0].attachment.content == b"csv"
    assert all(authorization == "Bearer " + "x" * 32 for _, authorization in transport.calls)


def test_token_provider_failure_is_redacted_and_classified(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BrokenToken:
        def get_access_token(self) -> str:
            raise RuntimeError("secret-marker")

    transport = MappingTransport({})
    provider = MicrosoftGraphMailProvider(
        transport,
        BrokenToken(),
        mailbox="ops@example.test",
    )

    with pytest.raises(MailCollectorError) as caught:
        next(provider.iter_messages())

    assert caught.value.error_code == "MAIL_AUTH_UNAVAILABLE"
    assert "secret-marker" not in str(caught.value)
    assert "secret-marker" not in caplog.text


def test_untrusted_pagination_host_is_rejected() -> None:
    path = (
        "/v1.0/users/ops%40example.test/mailFolders/inbox/messages?%24top=20&"
        "%24select=id%2Csubject%2CreceivedDateTime%2ChasAttachments%2CbodyPreview"
    )
    transport = MappingTransport(
        {path: {"value": [], "@odata.nextLink": "https://evil.test/v1.0/next"}}
    )
    with pytest.raises(MailCollectorError, match="pagination host"):
        tuple(_provider(transport).iter_messages())


def test_attachment_pagination_is_rejected_instead_of_dropping_files() -> None:
    message_path = (
        "/v1.0/users/ops%40example.test/mailFolders/inbox/messages?%24top=20&"
        "%24select=id%2Csubject%2CreceivedDateTime%2ChasAttachments%2CbodyPreview"
    )
    attachment_path = "/v1.0/users/ops%40example.test/messages/m/attachments?%24top=32"
    transport = MappingTransport(
        {
            message_path: {
                "value": [
                    {"id": "m", "subject": "x", "receivedDateTime": "now", "hasAttachments": True}
                ]
            },
            attachment_path: {
                "value": [],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/next",
            },
        }
    )
    with pytest.raises(MailCollectorError, match="attachment page"):
        tuple(MailCollector(_provider(transport)).collect())


@pytest.mark.parametrize(
    "attachment",
    [
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "id": "a",
            "name": "../x",
            "contentBytes": "YQ==",
        },
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "id": "a",
            "name": "x.csv",
            "contentBytes": "%%%",
        },
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "id": "a",
            "name": "x.csv",
            "contentBytes": "YQ==",
            "size": 2,
        },
    ],
)
def test_unsafe_attachment_payload_is_rejected(attachment: dict[str, object]) -> None:
    message_path = (
        "/v1.0/users/ops%40example.test/mailFolders/inbox/messages?%24top=20&"
        "%24select=id%2Csubject%2CreceivedDateTime%2ChasAttachments%2CbodyPreview"
    )
    attachment_path = "/v1.0/users/ops%40example.test/messages/m/attachments?%24top=32"
    transport = MappingTransport(
        {
            message_path: {
                "value": [
                    {"id": "m", "subject": "x", "receivedDateTime": "now", "hasAttachments": True}
                ]
            },
            attachment_path: {"value": [attachment]},
        }
    )
    with pytest.raises(MailCollectorError):
        tuple(MailCollector(_provider(transport)).collect())


def test_provider_configuration_is_bounded() -> None:
    with pytest.raises(ValueError, match="between 1 and 50"):
        MicrosoftGraphMailProvider(MappingTransport({}), StaticToken(), mailbox="x", page_size=51)
    with pytest.raises(ValueError, match=GRAPH_HOST):
        MicrosoftGraphMailProvider(
            MappingTransport({}), StaticToken(), mailbox="x", graph_host="evil.test"
        )
