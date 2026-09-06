"""Every refusal the evidence-unlocker wire protocol makes.

The unlocker process holds the evidence key and has no database, no network and
no way to ask a second opinion: the frame it is handed is the whole of what it
knows.  So the protocol checks the frame twice - once through the field types,
and again in a model validator that does not trust them - and refuses anything
that is oversized, non-canonical, or shaped like a different message.  Each test
below names one of those refusals.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Callable
from typing import Any, cast
from uuid import UUID

import pytest

from ledgerbridge import evidence_unlocker_protocol as protocol
from ledgerbridge.evidence_unlocker_protocol import (
    MAX_UNLOCKER_REQUEST_BYTES,
    MAX_UNLOCKER_RESPONSE_BYTES,
    EvidenceUnlockerProtocolError,
    UnlockerOutputDescriptor,
    UnlockerRequest,
    UnlockerResponse,
    UnlockerSourceDescriptor,
    UnlockerStatus,
    _require_canonical_uuid,
    decode_unlocker_request,
    decode_unlocker_response,
    encode_unlocker_request,
    encode_unlocker_response,
)
from tests.test_evidence_unlocker_protocol import _source

NIL = "00000000-0000-0000-0000-000000000000"
# A valid UUID spelled in upper case: `UUID` accepts it, `str` gives back another
# spelling, and the protocol insists the two agree.
NON_CANONICAL = "2A000000-0000-4000-8000-00000000000B"
REQUEST_ID = UUID("25000000-0000-4000-8000-000000000001")
OPERATION_ID = UUID("26000000-0000-4000-8000-000000000001")
REQUEST_NONCE = UUID("27000000-0000-4000-8000-000000000001")
SOURCE_REF = UUID("21000000-0000-4000-8000-000000000001")
OUTPUT_REF = UUID("22000000-0000-4000-8000-000000000001")


def _source_fields(**overrides: Any) -> dict[str, Any]:
    """The descriptor the existing suite considers well formed, as raw fields."""
    fields: dict[str, Any] = dict(_source())
    fields.update(overrides)
    return fields


def _request(**overrides: Any) -> UnlockerRequest:
    fields: dict[str, Any] = {
        "request_id": REQUEST_ID,
        "operation_id": OPERATION_ID,
        "request_nonce": REQUEST_NONCE,
        "source": _source(),
        "password": "synthetic-one-request-password",
    }
    fields.update(overrides)
    return UnlockerRequest(**fields)


def _output_fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "evidence_ref": OUTPUT_REF,
        "media_type": "application/pdf",
        "display_name": "statement.pdf",
        "object_ref": "1" * 64,
        "plaintext_sha256": "2" * 64,
        "plaintext_size": 2048,
        "ciphertext_sha256": "3" * 64,
        "ciphertext_size": 2560,
        "storage_key": f"sha256/33/33/{'3' * 64}",
        "chunk_size": 65536,
        "stream_header": "4" * 48,
        "wrapped_key_generation": "test-v1",
        "wrapped_key_nonce": "5" * 48,
        "wrapped_key_ciphertext": "6" * 96,
    }
    fields.update(overrides)
    return fields


def _output(**overrides: Any) -> UnlockerOutputDescriptor:
    return UnlockerOutputDescriptor(**_output_fields(**overrides))


def _response(**overrides: Any) -> UnlockerResponse:
    fields: dict[str, Any] = {
        "request_id": REQUEST_ID,
        "operation_id": OPERATION_ID,
        "request_nonce": REQUEST_NONCE,
        "source_ref": SOURCE_REF,
        "status": UnlockerStatus.UNLOCKED,
        "outputs": (_output(),),
    }
    fields.update(overrides)
    return UnlockerResponse(**fields)


def _frame(payload: object, *, declared: int | None = None) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    size = len(body) if declared is None else declared
    return struct.pack("!I", size) + body


class _RefusesToSerialise:
    """Stands in for the JSON encoder when a payload cannot be serialised."""

    @staticmethod
    def dumps(*args: object, **kwargs: object) -> str:
        raise ValueError("payload is not serialisable")


# --- the source descriptor ---------------------------------------------------


@pytest.mark.parametrize("nil_field", ["source_ref", "evidence_ref"])
def test_a_source_without_an_identity_is_refused(nil_field: str) -> None:
    """A nil UUID names nothing, so it cannot name the evidence being unlocked."""
    with pytest.raises(ValueError, match="unlocker source identity is invalid"):
        UnlockerSourceDescriptor(**_source_fields(**{nil_field: NIL}))


def test_a_storage_key_that_is_not_a_content_address_is_refused() -> None:
    """The key must be the fan-out path the artifact store actually uses."""
    with pytest.raises(ValueError, match="unlocker storage key is invalid"):
        UnlockerSourceDescriptor(**_source_fields(storage_key=f"sha512/cc/cc/{'c' * 64}"))


def test_a_storage_key_addressing_other_ciphertext_is_refused() -> None:
    """A well-shaped key for a different digest would fetch the wrong blob."""
    with pytest.raises(ValueError, match="storage key does not match ciphertext"):
        UnlockerSourceDescriptor(**_source_fields(storage_key=f"sha256/dd/dd/{'d' * 64}"))


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("object_ref", "zz" + "a" * 62, "unlocker object reference is invalid"),
        ("plaintext_sha256", "zz" + "b" * 62, "unlocker plaintext digest is invalid"),
        ("ciphertext_sha256", "zz" + "c" * 62, "unlocker ciphertext digest is invalid"),
        ("stream_header", "zz" + "d" * 46, "unlocker stream header is invalid"),
        ("wrapped_key_nonce", "zz" + "e" * 46, "unlocker wrapped key nonce is invalid"),
        ("wrapped_key_ciphertext", "zz" + "f" * 94, "unlocker wrapped key ciphertext is invalid"),
    ],
)
def test_the_source_rechecks_its_own_fields(field_name: str, value: str, message: str) -> None:
    """The validator does not trust the field patterns: it matches every hex field again.

    `model_construct` is the only way to hand the validator a value the field
    types would have rejected, which is exactly the situation this second layer
    exists for.
    """
    built = UnlockerSourceDescriptor.model_construct(**_source_fields(**{field_name: value}))
    recheck = cast("Callable[[], object]", built.descriptor_is_bound)

    with pytest.raises(ValueError, match=message):
        recheck()


# --- the request -------------------------------------------------------------


@pytest.mark.parametrize("nil_field", ["request_id", "operation_id", "request_nonce"])
def test_a_request_without_an_identity_is_refused(nil_field: str) -> None:
    """Request, operation and nonce are what make a replay recognisable."""
    with pytest.raises(ValueError, match="unlocker request identity is invalid"):
        _request(**{nil_field: NIL})


def test_a_password_carrying_a_null_byte_is_refused() -> None:
    """A NUL would truncate the secret wherever it crosses a C boundary."""
    with pytest.raises(ValueError, match="unlocker password is invalid"):
        _request(password="secret\x00tail")


# --- the output descriptor ---------------------------------------------------


def test_an_output_without_an_identity_is_refused() -> None:
    """An output that names no evidence could not be filed against anything."""
    with pytest.raises(ValueError, match="unlocker output identity is invalid"):
        _output(evidence_ref=NIL)


@pytest.mark.parametrize(
    "display_name",
    [".", "..", "reports/statement.pdf", "reports\\statement.pdf", "statement\n.pdf", "bell\x07"],
)
def test_an_output_name_that_could_escape_its_directory_is_refused(display_name: str) -> None:
    """The name is written to disk by the caller, so it may not steer the path."""
    with pytest.raises(ValueError, match="unlocker output name is invalid"):
        _output(display_name=display_name)


@pytest.mark.parametrize("media_type", ["text/plain\r", "text/plain\n", "text/plain\x00"])
def test_an_output_media_type_that_could_inject_a_header_is_refused(media_type: str) -> None:
    """The media type reaches an HTTP header; a bare CR or LF would split it."""
    with pytest.raises(ValueError, match="unlocker output media type is invalid"):
        _output(media_type=media_type)


def test_an_output_storage_key_addressing_other_ciphertext_is_refused() -> None:
    """As with the source, the key must address the ciphertext it travels with."""
    with pytest.raises(ValueError, match="output storage key does not match ciphertext"):
        _output(storage_key=f"sha256/99/99/{'9' * 64}")


# --- the response ------------------------------------------------------------


@pytest.mark.parametrize(
    "nil_field",
    ["request_id", "operation_id", "request_nonce", "source_ref"],
)
def test_a_response_without_an_identity_is_refused(nil_field: str) -> None:
    """A response has to say which request it answers, or it answers all of them."""
    with pytest.raises(ValueError, match="unlocker response identity is invalid"):
        _response(**{nil_field: NIL})


def test_a_success_without_outputs_is_refused() -> None:
    """UNLOCKED with nothing unlocked would report a success that produced nothing."""
    with pytest.raises(ValueError, match="successful unlocker response is incomplete"):
        _response(outputs=())


def test_a_success_carrying_an_error_code_is_refused() -> None:
    """A success and a failure reason in one message leaves the caller to guess."""
    with pytest.raises(ValueError, match="successful unlocker response is incomplete"):
        _response(error_code="UNLOCK_REJECTED")


def test_a_failure_carrying_outputs_is_refused() -> None:
    """A rejected unlock must not smuggle plaintext descriptors back out."""
    with pytest.raises(ValueError, match="failed unlocker response is incomplete"):
        _response(status=UnlockerStatus.REJECTED, error_code="UNLOCK_REJECTED")


def test_a_failure_without_an_error_code_is_refused() -> None:
    """A failure with no reason cannot be turned into an audit record."""
    with pytest.raises(ValueError, match="failed unlocker response is incomplete"):
        _response(status=UnlockerStatus.ERROR, outputs=())


# --- encoding ----------------------------------------------------------------


def test_encoding_refuses_anything_but_the_exact_request_type() -> None:
    """A subclass could carry extra fields the unlocker never agreed to read."""

    class _WiderRequest(UnlockerRequest):
        pass

    wider = _WiderRequest(
        request_id=REQUEST_ID,
        operation_id=OPERATION_ID,
        request_nonce=REQUEST_NONCE,
        source=_source(),
        password="synthetic-one-request-password",
    )

    with pytest.raises(EvidenceUnlockerProtocolError, match="request type is invalid"):
        encode_unlocker_request(wider)


def test_encoding_refuses_a_request_the_json_encoder_will_not_take(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If serialisation fails the frame is abandoned, not sent half-formed."""
    monkeypatch.setattr(protocol, "json", _RefusesToSerialise)

    with pytest.raises(EvidenceUnlockerProtocolError, match="request is not bounded JSON"):
        encode_unlocker_request(_request())


def test_encoding_refuses_a_request_over_its_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unlocker reads a bounded frame, so the sender must not exceed it."""
    monkeypatch.setattr(protocol, "MAX_UNLOCKER_REQUEST_BYTES", 8)

    with pytest.raises(EvidenceUnlockerProtocolError, match="request exceeds its byte limit"):
        encode_unlocker_request(_request())


def test_encoding_refuses_anything_but_the_exact_response_type() -> None:
    """The caller trusts the response shape, so only the declared type is encoded."""

    class _WiderResponse(UnlockerResponse):
        pass

    wider = _WiderResponse(
        request_id=REQUEST_ID,
        operation_id=OPERATION_ID,
        request_nonce=REQUEST_NONCE,
        source_ref=SOURCE_REF,
        status=UnlockerStatus.UNLOCKED,
        outputs=(_output(),),
    )

    with pytest.raises(EvidenceUnlockerProtocolError, match="response type is invalid"):
        encode_unlocker_response(wider)


def test_encoding_refuses_a_response_the_json_encoder_will_not_take(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A response that cannot be serialised is an error, not an empty frame."""
    monkeypatch.setattr(protocol, "json", _RefusesToSerialise)

    with pytest.raises(EvidenceUnlockerProtocolError, match="response is not bounded JSON"):
        encode_unlocker_response(_response())


def test_encoding_refuses_a_response_over_its_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller reads a bounded frame too; the unlocker may not overrun it."""
    monkeypatch.setattr(protocol, "MAX_UNLOCKER_RESPONSE_BYTES", 8)

    with pytest.raises(EvidenceUnlockerProtocolError, match="response exceeds its byte limit"):
        encode_unlocker_response(_response())


# --- decoding a request ------------------------------------------------------


@pytest.mark.parametrize("frame", [b"", b"abc", "not bytes at all"])
def test_a_request_frame_without_a_length_prefix_is_refused(frame: object) -> None:
    """Four bytes of length come first; anything shorter is a truncated read."""
    with pytest.raises(EvidenceUnlockerProtocolError, match="request frame is truncated"):
        decode_unlocker_request(frame)  # type: ignore[arg-type]


def test_a_request_frame_whose_length_does_not_match_its_body_is_refused() -> None:
    """A declared length longer than the body means the stream was cut short."""
    with pytest.raises(EvidenceUnlockerProtocolError, match="request frame length is invalid"):
        decode_unlocker_request(_frame(dict(_request().model_dump(mode="json")), declared=9))


def test_a_request_body_that_is_not_an_object_is_refused() -> None:
    """A JSON array cannot carry named fields, so it cannot be a request."""
    with pytest.raises(EvidenceUnlockerProtocolError, match="unlocker request is invalid"):
        decode_unlocker_request(_frame(["request_id", "operation_id"]))


@pytest.mark.parametrize("field_name", ["request_id", "operation_id", "request_nonce"])
def test_a_request_uuid_that_is_not_canonical_is_refused(field_name: str) -> None:
    """Two spellings of one UUID would make the same request replayable twice."""
    payload = _request().model_dump(mode="json")
    payload[field_name] = NON_CANONICAL

    with pytest.raises(EvidenceUnlockerProtocolError, match="unlocker request is invalid"):
        decode_unlocker_request(_frame(payload))


@pytest.mark.parametrize("field_name", ["source_ref", "evidence_ref"])
def test_a_source_uuid_that_is_not_canonical_is_refused(field_name: str) -> None:
    """The nested source is held to the same canonical spelling as the request."""
    payload = _request().model_dump(mode="json")
    source = payload["source"]
    assert isinstance(source, dict)
    source[field_name] = NON_CANONICAL

    with pytest.raises(EvidenceUnlockerProtocolError, match="unlocker request is invalid"):
        decode_unlocker_request(_frame(payload))


# --- decoding a response -----------------------------------------------------


@pytest.mark.parametrize("frame", [b"", b"abc", "not bytes at all"])
def test_a_response_frame_without_a_length_prefix_is_refused(frame: object) -> None:
    """The response frame is read the same way, so it is refused the same way."""
    with pytest.raises(EvidenceUnlockerProtocolError, match="response frame is truncated"):
        decode_unlocker_response(frame)  # type: ignore[arg-type]


def test_a_response_frame_declaring_more_than_the_limit_is_refused() -> None:
    """The length is refused before the body is read, so no oversized read happens."""
    with pytest.raises(EvidenceUnlockerProtocolError, match="response exceeds its byte limit"):
        decode_unlocker_response(struct.pack("!I", MAX_UNLOCKER_RESPONSE_BYTES + 1))


def test_a_response_frame_whose_length_does_not_match_its_body_is_refused() -> None:
    """A body shorter than its own declared length is a truncated response."""
    with pytest.raises(EvidenceUnlockerProtocolError, match="response frame length is invalid"):
        decode_unlocker_response(_frame(_response().model_dump(mode="json"), declared=9))


def test_a_response_body_that_is_not_an_object_is_refused() -> None:
    """A JSON array cannot carry a status, so it cannot be a response."""
    with pytest.raises(EvidenceUnlockerProtocolError, match="unlocker response is invalid"):
        decode_unlocker_response(_frame(["UNLOCKED"]))


def test_a_response_that_omits_its_outputs_entirely_is_refused() -> None:
    """The model would default the field to empty; the frame must say so itself."""
    payload = _response(status=UnlockerStatus.ERROR, outputs=(), error_code="UNLOCKER_UNAVAILABLE")
    body = payload.model_dump(mode="json")
    del body["outputs"]

    with pytest.raises(EvidenceUnlockerProtocolError, match="unlocker response is invalid"):
        decode_unlocker_response(_frame(body))


@pytest.mark.parametrize(
    "field_name",
    ["request_id", "operation_id", "request_nonce", "source_ref"],
)
def test_a_response_uuid_that_is_not_canonical_is_refused(field_name: str) -> None:
    """A response spelled differently from its request could not be matched to it."""
    payload = _response().model_dump(mode="json")
    payload[field_name] = NON_CANONICAL

    with pytest.raises(EvidenceUnlockerProtocolError, match="unlocker response is invalid"):
        decode_unlocker_response(_frame(payload))


def test_an_output_uuid_that_is_not_canonical_is_refused() -> None:
    """Each output is checked too: an unlocked file has to name real evidence."""
    payload = _response().model_dump(mode="json")
    outputs = payload["outputs"]
    assert isinstance(outputs, list)
    outputs[0]["evidence_ref"] = NON_CANONICAL

    with pytest.raises(EvidenceUnlockerProtocolError, match="unlocker response is invalid"):
        decode_unlocker_response(_frame(payload))


def test_a_uuid_field_that_is_not_text_is_refused() -> None:
    """The canonical check reads the raw JSON value, which need not be a string."""
    with pytest.raises(ValueError, match="UUID field is not text"):
        _require_canonical_uuid({"request_id": 25}, "request_id")


def test_a_well_formed_request_still_round_trips() -> None:
    """None of the refusals above have closed the door on a valid message."""
    request = _request()

    assert decode_unlocker_request(encode_unlocker_request(request)) == request
    assert len(encode_unlocker_request(request)) <= MAX_UNLOCKER_REQUEST_BYTES
