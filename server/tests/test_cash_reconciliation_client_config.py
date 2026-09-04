from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from server.app import _build_cash_reconciliation_client


class CashReconciliationClientConfigTests(unittest.TestCase):
    def test_missing_or_partial_credentials_leave_only_monthly_reconciliation_unavailable(
        self,
    ) -> None:
        for environment in (
            {},
            {"CORE_CASH_RECONCILIATION_CERT_FILE": "/run/reconciliation/client.crt"},
            {"CORE_CASH_RECONCILIATION_KEY_FILE": "/run/reconciliation/client.key"},
        ):
            with self.subTest(environment=environment), patch.dict(
                os.environ,
                environment,
                clear=True,
            ), patch("server.app.CoreHttpClient") as client_type:
                client = _build_cash_reconciliation_client(
                    default_ca_file="/run/core/ca.crt",
                    timeout_seconds=10,
                )

                self.assertIsNone(client)
                client_type.assert_not_called()

    def test_dedicated_credentials_build_an_independent_client(self) -> None:
        environment = {
            "CORE_CASH_RECONCILIATION_CERT_FILE": "/run/reconciliation/client.crt",
            "CORE_CASH_RECONCILIATION_KEY_FILE": "/run/reconciliation/client.key",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "server.app.CoreHttpClient"
        ) as client_type:
            reconciliation_client = object()
            client_type.return_value = reconciliation_client

            result = _build_cash_reconciliation_client(
                default_ca_file="/run/core/ca.crt",
                timeout_seconds=8,
            )

            self.assertIs(result, reconciliation_client)
            client_type.assert_called_once_with(
                base_url="https://internal-ingress:8446",
                ca_file="/run/core/ca.crt",
                certificate_file="/run/reconciliation/client.crt",
                private_key_file="/run/reconciliation/client.key",
                timeout_seconds=8,
            )

    def test_invalid_dedicated_credentials_fail_closed_for_this_feature(self) -> None:
        environment = {
            "CORE_CASH_RECONCILIATION_CERT_FILE": "/run/reconciliation/client.crt",
            "CORE_CASH_RECONCILIATION_KEY_FILE": "/run/reconciliation/client.key",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "server.app.CoreHttpClient",
            side_effect=OSError("unreadable reconciliation credential"),
        ):
            result = _build_cash_reconciliation_client(
                default_ca_file="/run/core/ca.crt",
                timeout_seconds=10,
            )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
