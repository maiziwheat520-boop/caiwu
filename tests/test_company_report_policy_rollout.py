from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.company_report_policy_rollout import (
    ContainerIdentity,
    RolloutConfig,
    RolloutError,
    compose_recreate_command,
    execute_rollout,
    prepare_files,
    rollout_plan,
)

SENSITIVE_ENTITY = "10000000-0000-4000-8000-000000000001"
SENSITIVE_MARKER = "must-never-reach-runner-output"


def _config(tmp_path: Path) -> RolloutConfig:
    policy_path = tmp_path / "policy.json"
    core_env_path = tmp_path / "core.env"
    web_env_path = tmp_path / "web.env"
    policy_path.write_text(
        json.dumps(
            {
                "version": "ledgerbridge.mtls-workload-policy.v1",
                "certificate_serial": "A1B2",
                "policy_generation": 2,
                "principal": {
                    "principal_ref": SENSITIVE_MARKER,
                    "san_uri": "spiffe://ledgerbridge.local/web",
                    "policy_generation": 2,
                    "capabilities": ["candidate:read", "reconciliation:read"],
                    "grants": [
                        {
                            "entity_ref": SENSITIVE_ENTITY,
                            "include_unassigned": True,
                            "business_unit_refs": [],
                            "business_unit_ids": [],
                            "business_unit_bindings": [],
                        }
                    ],
                },
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    core_env_path.write_text(
        "LEDGERBRIDGE_INTERNAL_READ_POLICY_GENERATION=2\nDATABASE_URL=secret\n",
        encoding="utf-8",
    )
    web_env_path.write_text(
        "CORE_POLICY_GENERATION=2\nSESSION_KEY=secret\n",
        encoding="utf-8",
    )
    return RolloutConfig(
        policy_path=policy_path,
        core_env_path=core_env_path,
        web_env_path=web_env_path,
        backup_root=tmp_path / "backups",
        expected_generation=2,
        target_generation=3,
        core_compose_paths=(
            tmp_path / "core-compose.yml",
            tmp_path / "core-review.yml",
        ),
        web_compose_paths=(tmp_path / "web-compose.yml",),
        core_project_name="ledgerbridge",
        web_project_name="ledgerbridge-web-core",
        reader_service="internal-reader",
        web_service="web",
        reader_container="ledgerbridge-internal-reader-1",
        web_container="ledgerbridge-web-core",
    )


class FakeRuntime:
    def __init__(
        self,
        *,
        fail_health_once: str | None = None,
        drift_non_target: bool = False,
    ) -> None:
        self.fail_health_once = fail_health_once
        self.drift_non_target = drift_non_target
        self.recreated: list[str] = []
        self.identities = {
            "ledgerbridge-internal-reader-1": ContainerIdentity(
                "reader-old", "image-core", "t1", 0
            ),
            "ledgerbridge-web-core": ContainerIdentity("web-old", "image-web", "t1", 0),
            "ledgerbridge-postgres-1": ContainerIdentity("db-stable", "image-db", "t1", 0),
        }

    def snapshot_containers(self) -> dict[str, ContainerIdentity]:
        return dict(self.identities)

    def recreate(self, role: str) -> None:
        self.recreated.append(role)
        if role == "reader":
            self.identities["ledgerbridge-internal-reader-1"] = ContainerIdentity(
                f"reader-{len(self.recreated)}", "image-core", "t2", 0
            )
            if self.drift_non_target:
                self.identities["ledgerbridge-postgres-1"] = ContainerIdentity(
                    "db-changed", "image-db", "t2", 0
                )
        else:
            self.identities["ledgerbridge-web-core"] = ContainerIdentity(
                f"web-{len(self.recreated)}", "image-web", "t2", 0
            )

    def wait_healthy(self, role: str) -> None:
        if self.fail_health_once == role:
            self.fail_health_once = None
            raise RolloutError(f"{role.upper()}_HEALTH_FAILED")

    def assert_generation(self, role: str, expected: int) -> None:
        assert expected in {2, 3}


class CompanyReportPolicyRolloutTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.config = _config(Path(self.temporary_directory.name))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_prepare_files_changes_only_capability_and_bound_generations(self) -> None:
        prepared = prepare_files(self.config)

        original_policy = json.loads(self.config.policy_path.read_text(encoding="utf-8"))
        updated_policy = json.loads(prepared[self.config.policy_path])
        self.assertEqual(updated_policy["policy_generation"], 3)
        self.assertEqual(updated_policy["principal"]["policy_generation"], 3)
        self.assertEqual(
            set(updated_policy["principal"]["capabilities"]),
            {"candidate:read", "reconciliation:read", "ledger:read"},
        )
        updated_policy["policy_generation"] = 2
        updated_policy["principal"]["policy_generation"] = 2
        updated_policy["principal"]["capabilities"].remove("ledger:read")
        self.assertEqual(updated_policy, original_policy)
        self.assertIn(
            "LEDGERBRIDGE_INTERNAL_READ_POLICY_GENERATION=3",
            prepared[self.config.core_env_path],
        )
        self.assertIn("CORE_POLICY_GENERATION=3", prepared[self.config.web_env_path])

    def test_plan_is_non_mutating_and_never_discloses_policy_or_entity_values(self) -> None:
        original = self.config.policy_path.read_bytes()

        rendered = "\n".join(rollout_plan(self.config))

        self.assertEqual(self.config.policy_path.read_bytes(), original)
        self.assertNotIn(SENSITIVE_ENTITY, rendered)
        self.assertNotIn(SENSITIVE_MARKER, rendered)
        self.assertNotIn("policy payload", rendered.lower())
        self.assertIn("2 -> 3", rendered)
        self.assertIn("backup", rendered.lower())
        self.assertIn("rollback", rendered.lower())

    def test_reader_recreate_preserves_the_complete_compose_file_sequence(self) -> None:
        command = compose_recreate_command(self.config, "reader")

        self.assertEqual(command[2:4], ("--project-name", "ledgerbridge"))
        self.assertEqual(command.count("-f"), 2)
        self.assertLess(
            command.index(str(self.config.core_compose_paths[0])),
            command.index(str(self.config.core_compose_paths[1])),
        )
        self.assertEqual(command[-3:], ("--no-deps", "--force-recreate", "internal-reader"))

    def test_web_recreate_targets_the_existing_web_compose_project(self) -> None:
        command = compose_recreate_command(self.config, "web")

        self.assertEqual(command[2:4], ("--project-name", "ledgerbridge-web-core"))
        self.assertEqual(command[-3:], ("--no-deps", "--force-recreate", "web"))

    def test_failed_web_health_restores_all_files_and_recreates_both_old_generations(self) -> None:
        originals = {
            path: path.read_bytes()
            for path in (
                self.config.policy_path,
                self.config.core_env_path,
                self.config.web_env_path,
            )
        }
        runtime = FakeRuntime(fail_health_once="web")
        emitted: list[str] = []

        with self.assertRaisesRegex(RolloutError, "WEB_HEALTH_FAILED"):
            execute_rollout(self.config, runtime=runtime, emit=emitted.append)

        self.assertEqual({path: path.read_bytes() for path in originals}, originals)
        self.assertEqual(runtime.recreated, ["reader", "web", "reader", "web"])
        self.assertTrue(any(self.config.backup_root.iterdir()))
        rendered = "\n".join(emitted)
        self.assertNotIn(SENSITIVE_ENTITY, rendered)
        self.assertNotIn(SENSITIVE_MARKER, rendered)

    def test_successful_rollout_updates_both_generations_and_preserves_non_targets(self) -> None:
        runtime = FakeRuntime()
        emitted: list[str] = []

        backup_dir = execute_rollout(
            self.config,
            runtime=runtime,
            emit=emitted.append,
        )

        self.assertTrue((backup_dir / "manifest.json").is_file())
        policy = json.loads(self.config.policy_path.read_text(encoding="utf-8"))
        self.assertEqual(policy["policy_generation"], 3)
        self.assertIn("ledger:read", policy["principal"]["capabilities"])
        self.assertIn(
            "LEDGERBRIDGE_INTERNAL_READ_POLICY_GENERATION=3",
            self.config.core_env_path.read_text(encoding="utf-8"),
        )
        self.assertIn(
            "CORE_POLICY_GENERATION=3",
            self.config.web_env_path.read_text(encoding="utf-8"),
        )
        self.assertEqual(runtime.recreated, ["reader", "web"])
        self.assertIn("NON_TARGET_IDENTITIES_STABLE", emitted)
        self.assertIn("ROLLOUT_COMMITTED generation 3", emitted)

    def test_non_target_container_identity_drift_forces_rollback(self) -> None:
        runtime = FakeRuntime(drift_non_target=True)

        with self.assertRaisesRegex(RolloutError, "NON_TARGET_CONTAINER_DRIFT"):
            execute_rollout(self.config, runtime=runtime, emit=lambda _: None)

        self.assertEqual(runtime.recreated, ["reader", "web", "reader", "web"])
