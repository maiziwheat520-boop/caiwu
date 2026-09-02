from __future__ import annotations

import hashlib
from uuid import UUID

from ledgerbridge.internal_command_assertion import UserAssertionClaims


def test_bank_statement_review_path_is_allowed_in_user_assertion() -> None:
    statement_ref = UUID("d7a47e34-d174-55cc-9db2-993df8ca8261")

    claims = UserAssertionClaims(
        issuer="ledgerbridge-web",
        audience="ledgerbridge-core",
        subject="owner-passkey-1",
        authentication_generation=1,
        canonical_path=f"/internal/v1/bank-statements/{statement_ref}/reviews",
        body_sha256=hashlib.sha256(b"{}").hexdigest(),
        resource_ref=statement_ref,
        expected_revision=1,
        operation_id=UUID("10000000-0000-4000-8000-000000000001"),
        workload_principal="workload:ledgerbridge-web",
        policy_generation=5,
        issued_at=1,
        expires_at=46,
        jti=UUID("20000000-0000-4000-8000-000000000002"),
    )

    assert claims.canonical_path.endswith(f"/{statement_ref}/reviews")
