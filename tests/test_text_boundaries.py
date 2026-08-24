from __future__ import annotations

import pytest

from ledgerbridge.imports import IngestMetadata


@pytest.mark.parametrize("field", ["original_filename", "media_type"])
@pytest.mark.parametrize("bad_text", ["\x00", "\ud800"])
def test_ingest_metadata_rejects_unstorable_text(field: str, bad_text: str) -> None:
    values = {
        "source": "synthetic_upload",
        "original_filename": "synthetic.txt",
        "media_type": "text/plain",
    }
    values[field] = f"safe{bad_text}text"
    with pytest.raises(ValueError, match=field):
        IngestMetadata(**values)
