from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.app import _build_company_bank_review_client, _company_bank_statement_mappings


class CompanyBankReviewConfigTests(unittest.TestCase):
    def test_dedicated_credentials_use_the_8445_client(self) -> None:
        environment = {
            "CORE_COMPANY_BANK_REVIEW_CERT_FILE": "/run/review/client.crt",
            "CORE_COMPANY_BANK_REVIEW_KEY_FILE": "/run/review/client.key",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "server.app.CoreHttpClient"
        ) as client_type:
            _build_company_bank_review_client(
                default_ca_file="/run/core/ca.crt", timeout_seconds=8
            )
        client_type.assert_called_once_with(
            base_url="https://internal-ingress:8445",
            ca_file="/run/core/ca.crt",
            certificate_file="/run/review/client.crt",
            private_key_file="/run/review/client.key",
            timeout_seconds=8,
        )

    def test_file_mapping_accepts_seven_statements_and_never_requires_company_names(self) -> None:
        mappings = [
            {
                "statement_ref": f"10000000-0000-4000-8000-{index:012d}",
                "entity_ref": f"20000000-0000-4000-8000-{index:012d}",
            }
            for index in range(1, 8)
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "statements.json"
            path.write_text(json.dumps(mappings), encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "CORE_COMPANY_BANK_STATEMENTS_FILE": str(path),
                    "CORE_COMPANY_BANK_STATEMENTS_JSON": "not-json",
                },
                clear=True,
            ):
                result = _company_bank_statement_mappings()
        self.assertEqual(len(result), 7)
        self.assertEqual(result[0][2], "公司 1")
        self.assertEqual(result[-1][2], "公司 7")

    def test_file_mapping_rejects_more_than_thirty_two_statements(self) -> None:
        mappings = [
            {
                "statement_ref": f"10000000-0000-4000-8000-{index:012d}",
                "entity_ref": f"20000000-0000-4000-8000-{index:012d}",
            }
            for index in range(1, 34)
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "statements.json"
            path.write_text(json.dumps(mappings), encoding="utf-8")
            with patch.dict(
                os.environ,
                {"CORE_COMPANY_BANK_STATEMENTS_FILE": str(path)},
                clear=True,
            ):
                with self.assertRaises(SystemExit):
                    _company_bank_statement_mappings()


if __name__ == "__main__":
    unittest.main()
