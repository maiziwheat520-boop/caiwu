"""Run offline OCR over bounded bill images and write private review observations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ledgerbridge.bill_preprocessing import RapidOcrEngine, preprocess_image


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    image_directory = args.image_directory.resolve()
    if not image_directory.is_dir():
        raise SystemExit("image directory is unavailable")
    images = sorted(
        (
            path
            for path in image_directory.iterdir()
            if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
        ),
        key=lambda path: path.name.casefold(),
    )
    if not images or len(images) > 100:
        raise SystemExit("image count is out of bounds")
    engine = RapidOcrEngine()
    results = [asdict(preprocess_image(path, engine)) for path in images]
    payload: dict[str, Any] = {
        "schema_version": "ledgerbridge.bill-ocr.v1",
        "engine": "rapidocr-3.9.2/onnxruntime-1.29.0",
        "results": results,
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )
    _write_private(args.output.resolve(), encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    bill_count = sum(len(result["bills"]) for result in results)
    ready_count = sum(1 for result in results for bill in result["bills"] if not bill["blockers"])
    print(
        "BILL_OCR_OK "
        f"images={len(images)} bills={bill_count} review_ready={ready_count} sha256={digest}"
    )
    return 0


def _write_private(path: Path, encoded: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("OCR output write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
