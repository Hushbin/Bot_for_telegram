"""Connection timings must preserve driver outcomes, ownership and transactions."""

import json
import logging
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import database


class BrokenHandler(logging.Handler):
    def emit(self, record):
        raise RuntimeError("logging sink unavailable")


class ConnectionMetricsTests(unittest.TestCase):
    def make_store(self):
        return database.OracleStore(SimpleNamespace(
            db_user="private-user", db_password="private-password", db_dsn="private-dsn",
            db_wallet_dir="/private-wallet", db_wallet_password="private-wallet-password"))

    def event(self, logs):
        self.assertEqual(len(logs.records), 1)
        record = logs.records[0]
        self.assertEqual(record.levelno, logging.INFO)
        self.assertIsNone(record.exc_info)
        self.assertIsNone(record.stack_info)
        raw = record.getMessage()
        self.assertNotIn("\n", raw)
        return json.loads(raw.removeprefix("DB_CONNECTION "))

    def test_success_returns_the_exact_driver_connection_without_touching_it(self):
        store = self.make_store()
        connection = MagicMock()
        with patch.object(database.oracledb, "connect", return_value=connection) as connect:
            with patch.object(database, "monotonic_ns", side_effect=[1_000_000_000, 1_125_500_000]):
                with self.assertLogs("database", level="INFO") as logs:
                    result = store.connect()
        self.assertIs(result, connection)
        connect.assert_called_once_with(**store.connection_args)
        self.assertEqual(connection.mock_calls, [])
        self.assertEqual(self.event(logs), dict(v=1, outcome="success", elapsed_ms=125.5))
        for value in store.connection_args.values():
            self.assertNotIn(value, " ".join(logs.output))

    def test_failure_preserves_the_original_exception_and_only_logs_its_class(self):
        store = self.make_store()
        error = database.oracledb.DatabaseError("private-password SQL bind=user-contact private-dsn")
        with patch.object(database.oracledb, "connect", side_effect=error) as connect:
            with patch.object(database, "monotonic_ns", side_effect=[0, 25_000_000]):
                with self.assertLogs("database", level="INFO") as logs:
                    with self.assertRaises(database.oracledb.DatabaseError) as caught:
                        store.connect()
        self.assertIs(caught.exception, error)
        connect.assert_called_once_with(**store.connection_args)
        self.assertEqual(self.event(logs), dict(v=1, outcome="failure", elapsed_ms=25.0,
                                               error_type="DatabaseError"))
        for secret in ("private-password", "user-contact", "SQL", "private-dsn"):
            self.assertNotIn(secret, " ".join(logs.output))

    def test_failing_log_handler_cannot_lose_a_connection_or_mask_a_driver_error(self):
        logger = database.LOG
        old_level = logger.level
        logger.setLevel(logging.INFO)
        handler = BrokenHandler()
        logger.addHandler(handler)
        self.addCleanup(logger.setLevel, old_level)
        self.addCleanup(logger.removeHandler, handler)
        connection = MagicMock()
        error = RuntimeError("original driver failure")
        with patch.object(database.oracledb, "connect", side_effect=[connection, error]) as connect:
            self.assertIs(self.make_store().connect(), connection)
            with self.assertRaises(RuntimeError) as caught:
                self.make_store().connect()
        self.assertIs(caught.exception, error)
        self.assertEqual(connection.mock_calls, [])
        self.assertEqual(connect.call_count, 2)

    def test_timing_failure_after_open_does_not_lose_the_connection(self):
        connection = MagicMock()
        with patch.object(database.oracledb, "connect", return_value=connection):
            with patch.object(database, "monotonic_ns", side_effect=[0, RuntimeError("clock error")]):
                self.assertIs(self.make_store().connect(), connection)
        self.assertEqual(connection.mock_calls, [])

    def test_timing_failure_before_open_does_not_change_the_driver_outcome(self):
        connection = MagicMock()
        error = database.oracledb.DatabaseError("original driver failure")
        with patch.object(database, "monotonic_ns", side_effect=RuntimeError("clock error")):
            with patch.object(database.oracledb, "connect", side_effect=[connection, error]) as connect:
                self.assertIs(self.make_store().connect(), connection)
                with self.assertRaises(database.oracledb.DatabaseError) as caught:
                    self.make_store().connect()
        self.assertIs(caught.exception, error)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(connection.mock_calls, [])

    def test_existing_order_transaction_commit_rollback_and_cleanup_are_preserved(self):
        for write, fail in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(write=write, fail=fail):
                store = self.make_store()
                connection = MagicMock()
                connection.__enter__.return_value = connection

                def exit_connection(*args):
                    connection.close()
                    return False

                connection.__exit__.side_effect = exit_connection
                cursor = connection.cursor.return_value
                error = RuntimeError("transaction body failed")
                with patch.object(database.oracledb, "connect", return_value=connection) as connect:
                    with self.assertLogs("database", level="INFO") as logs:
                        assertion = self.assertRaises(RuntimeError) if fail else nullcontext()
                        with assertion as caught:
                            with store.order_transaction(write=write) as db:
                                self.assertIs(db.connection, connection)
                                db.execute("SELECT 1 FROM dual")
                                if fail:
                                    raise error
                        if fail:
                            self.assertIs(caught.exception, error)
                connect.assert_called_once_with(**store.connection_args)
                self.assertEqual(self.event(logs)["outcome"], "success")
                self.assertEqual(connection.commit.call_count, int(write and not fail))
                self.assertEqual(connection.rollback.call_count, int(fail))
                cursor.execute.assert_called_once_with("SELECT 1 FROM dual", {})
                cursor.close.assert_called_once_with()
                connection.__exit__.assert_called_once()
                connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
