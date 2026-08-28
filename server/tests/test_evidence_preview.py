from __future__ import annotations

import unittest
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from server.evidence_preview import EvidencePreviewError, build_evidence_preview


def workbook_bytes() -> bytes:
    target = BytesIO()
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
              xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets><sheet name="中行待复核" sheetId="1" r:id="rId1"/></sheets>
            </workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Target="worksheets/sheet1.xml"
                Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>
            </Relationships>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
              <row r="4">
                <c r="A4" t="inlineStr"><is><t>清单ID</t></is></c>
                <c r="B4" t="inlineStr"><is><t>交易时间</t></is></c>
                <c r="C4" t="inlineStr"><is><t>金额(元)</t></is></c>
                <c r="D4" t="inlineStr"><is><t>对方名称</t></is></c>
              </row>
              <row r="26">
                <c r="A26" t="inlineStr"><is><t>TX-0139</t></is></c>
                <c r="B26"><v>46157.63733796297</v></c>
                <c r="C26"><v>80000</v></c>
                <c r="D26" t="inlineStr"><is><t>陈明哲</t></is></c>
              </row>
            </sheetData></worksheet>""",
        )
    return target.getvalue()


class EvidencePreviewTests(unittest.TestCase):
    def test_builds_direct_spreadsheet_record_for_candidate_reference(self) -> None:
        preview = build_evidence_preview(
            {
                "content": workbook_bytes(),
                "filename": "boc-manual-review.xlsx",
                "content_type": "application/octet-stream",
            },
            reference="TX-0139",
        )

        self.assertEqual(preview["kind"], "spreadsheet")
        self.assertTrue(preview["matched"])
        record = preview["records"][0]
        self.assertEqual(record["sheet"], "中行待复核")
        self.assertEqual(record["row_number"], 26)
        fields = {item["label"]: item["value"] for item in record["fields"]}
        self.assertEqual(fields["清单ID"], "TX-0139")
        self.assertEqual(fields["金额(元)"], "¥80,000.00")
        self.assertEqual(fields["对方名称"], "陈明哲")
        self.assertRegex(fields["交易时间"], r"^2026-05-")

    def test_sniffs_image_bytes_instead_of_trusting_filename(self) -> None:
        preview = build_evidence_preview(
            {"content": b"\x89PNG\r\n\x1a\nimage", "filename": "evidence.bin"},
            reference="PHOTO-1",
        )
        self.assertEqual(preview["kind"], "image")
        self.assertEqual(preview["media_type"], "image/png")
        self.assertTrue(preview["data_url"].startswith("data:image/png;base64,"))

    def test_rejects_unbounded_reference_before_reading_content(self) -> None:
        with self.assertRaises(EvidencePreviewError):
            build_evidence_preview(
                {"content": b"hello", "filename": "message.txt"},
                reference="../not-safe",
            )

    def test_falls_back_to_download_for_unsupported_active_content(self) -> None:
        preview = build_evidence_preview(
            {"content": b"<html><script>alert(1)</script></html>", "filename": "evidence.html"},
            reference="TX-0139",
        )
        self.assertEqual(preview["kind"], "unsupported")


if __name__ == "__main__":
    unittest.main()
