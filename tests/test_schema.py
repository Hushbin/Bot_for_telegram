"""The Oracle schema file and the in-place column migration must agree."""

import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_setup_db():
    spec = importlib.util.spec_from_file_location("setup_db", ROOT / "scripts" / "setup_db.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def schema_columns():
    """Map TABLE -> set of COLUMN names declared in db/schema.sql."""
    sql = "\n".join(line for line in (ROOT / "db" / "schema.sql").read_text().splitlines()
                    if not line.lstrip().startswith("--"))
    tables = {}
    for name, body in re.findall(r"CREATE TABLE (\w+) \((.*?)\n\);", sql, re.S):
        columns = set()
        for line in body.splitlines():
            match = re.match(r"    (\w+) ", line)
            if match:
                columns.add(match.group(1).upper())
        tables[name.upper()] = columns
    return tables


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.setup_db = load_setup_db()

    def test_every_migration_column_is_in_the_schema_file(self):
        declared = schema_columns()
        for table, column, _ in self.setup_db.COLUMN_MIGRATIONS:
            self.assertIn(column, declared[table], f"{table}.{column} missing from schema.sql")

    def test_old_tables_get_exactly_the_missing_columns(self):
        old = {"USERS": {"TELEGRAM_USER_ID", "FULL_NAME", "EMAIL", "PHONE", "ROLE"},
               "SELLERS": {"TELEGRAM_USER_ID", "BUSINESS_NAME"},
               "PRODUCTS": {"PRODUCT_ID", "DELETED_AT"}}
        statements = self.setup_db.missing_column_statements(old)
        self.assertEqual(len(statements), 4)
        self.assertTrue(all(s.startswith("ALTER TABLE ") for s in statements))
        self.assertIn("ALTER TABLE USERS ADD (PHONE_VERIFIED ", " ".join(statements))
        self.assertNotIn("PRODUCTS ADD (DELETED_AT", " ".join(statements))

    def test_current_and_not_yet_created_tables_need_no_migration(self):
        current = {table: set(columns) for table, columns in schema_columns().items()}
        self.assertEqual(self.setup_db.missing_column_statements(current), [])
        self.assertEqual(self.setup_db.missing_column_statements({}), [])


class OracleStartupCheckTests(unittest.TestCase):
    """OracleStore.check() needs a real database to run; these cover its decisions."""

    def make_store(self):
        from types import SimpleNamespace
        from database import OracleStore
        return OracleStore(SimpleNamespace(db_user="u", db_password="p", db_dsn="d",
                                           db_wallet_dir="", db_wallet_password=""))

    def test_check_asks_for_every_column_the_migration_adds(self):
        from unittest.mock import MagicMock, patch
        setup_db = load_setup_db()
        connection = MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        with patch("database.oracledb.connect", return_value=connection):
            self.make_store().check()
        asked = " ".join(call.args[0].upper() for call in cursor.execute.call_args_list)
        for table, column, _ in setup_db.COLUMN_MIGRATIONS:
            self.assertIn(column, asked, f"check() never selects {table}.{column}")

    def test_an_outdated_schema_stops_startup_with_a_pointer_to_the_fix(self):
        from unittest.mock import MagicMock, patch
        import oracledb
        connection = MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
        cursor.execute.side_effect = oracledb.DatabaseError("ORA-00904: invalid identifier")
        with patch("database.oracledb.connect", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "setup_db.py"):
                self.make_store().check()

    def test_wallet_arguments_are_only_sent_when_a_wallet_is_configured(self):
        self.assertNotIn("wallet_location", self.make_store().connection_args)
        from types import SimpleNamespace
        from database import OracleStore
        walleted = OracleStore(SimpleNamespace(db_user="u", db_password="p", db_dsn="d",
                                               db_wallet_dir="/w", db_wallet_password="x"))
        self.assertEqual(walleted.connection_args["wallet_location"], "/w")


if __name__ == "__main__":
    unittest.main()
