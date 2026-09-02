from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from server.app import _build_company_report_client


class CompanyReportClientConfigTests(unittest.TestCase):
    def test_missing_or_partial_credentials_leave_only_reports_unavailable(self) -> None:
        for environment in (
            {},
            {"CORE_COMPANY_REPORT_CERT_FILE": "/run/report/client.crt"},
            {"CORE_COMPANY_REPORT_KEY_FILE": "/run/report/client.key"},
        ):
            with self.subTest(environment=environment), patch.dict(
                os.environ,
                environment,
                clear=True,
            ), patch("server.app.CoreHttpClient") as client_type:
                client = _build_company_report_client(
                    default_base_url="https://core.test",
                    default_ca_file="/run/core/ca.crt",
                    timeout_seconds=10,
                )

                self.assertIsNone(client)
                client_type.assert_not_called()

    def test_dedicated_credentials_build_an_independent_client(self) -> None:
        environment = {
            "CORE_COMPANY_REPORT_CERT_FILE": "/run/report/client.crt",
            "CORE_COMPANY_REPORT_KEY_FILE": "/run/report/client.key",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "server.app.CoreHttpClient"
        ) as client_type:
            report_client = object()
            client_type.return_value = report_client

            result = _build_company_report_client(
                default_base_url="https://core.test",
                default_ca_file="/run/core/ca.crt",
                timeout_seconds=8,
            )

            self.assertIs(result, report_client)
            client_type.assert_called_once_with(
                base_url="https://core.test",
                ca_file="/run/core/ca.crt",
                certificate_file="/run/report/client.crt",
                private_key_file="/run/report/client.key",
                timeout_seconds=8,
            )

    def test_invalid_dedicated_credentials_do_not_block_web_startup(self) -> None:
        environment = {
            "CORE_COMPANY_REPORT_CERT_FILE": "/run/report/client.crt",
            "CORE_COMPANY_REPORT_KEY_FILE": "/run/report/client.key",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "server.app.CoreHttpClient",
            side_effect=OSError("unreadable report credential"),
        ):
            result = _build_company_report_client(
                default_base_url="https://core.test",
                default_ca_file="/run/core/ca.crt",
                timeout_seconds=10,
            )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
