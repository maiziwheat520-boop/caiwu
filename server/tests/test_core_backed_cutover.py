from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from server.core_backed_cutover import AUTH_TABLES, PREVIEW_TABLES, prepare_core_backed_state


class CoreBackedCutoverTests(unittest.TestCase):
    def test_backs_up_preview_database_and_preserves_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "web.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                for table in AUTH_TABLES:
                    connection.execute(f'CREATE TABLE "{table}" (value TEXT)')
                    connection.execute(f'INSERT INTO "{table}" VALUES (?)', (f"auth-{table}",))
                for table in PREVIEW_TABLES:
                    connection.execute(f'CREATE TABLE "{table}" (value TEXT)')
                    connection.execute(f'INSERT INTO "{table}" VALUES (?)', (f"preview-{table}",))
                connection.commit()

            result = prepare_core_backed_state(database, root / "backups")

            self.assertTrue(result.backup_path.is_file())
            self.assertEqual(result.removed_rows, len(PREVIEW_TABLES))
            self.assertEqual(result.preserved_auth_rows, len(AUTH_TABLES))
            with closing(sqlite3.connect(database)) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertTrue(set(AUTH_TABLES) <= tables)
                self.assertFalse(set(PREVIEW_TABLES) & tables)
                self.assertEqual(
                    connection.execute('SELECT value FROM "auth_user"').fetchone()[0],
                    "auth-auth_user",
                )
            with closing(sqlite3.connect(result.backup_path)) as backup:
                self.assertEqual(
                    backup.execute('SELECT value FROM "candidates"').fetchone()[0],
                    "preview-candidates",
                )


if __name__ == "__main__":
    unittest.main()
