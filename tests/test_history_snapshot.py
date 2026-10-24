"""Read-only status snapshots; lock concurrency requires native PostgreSQL."""
import os
from pathlib import Path
import sys
import time
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/backend/src"))
from center_messenger.accounts import Accounts
from center_messenger.config import Settings


@unittest.skipUnless(os.environ.get("CM_TEST_DATABASE_URL"), "isolated PostgreSQL URL required")
class HistorySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(os.environ["CM_TEST_DATABASE_URL"])
        self.addCleanup(self.engine.dispose)
        self.accounts = Accounts(self.engine, Settings())

    def test_snapshot_is_read_only_and_uses_repeatable_read(self):
        with self.accounts.transaction(snapshot=True) as db:
            self.assertEqual(db.execute(text("SHOW transaction_isolation")).scalar(), "repeatable read")
            self.assertEqual(db.execute(text("SHOW transaction_read_only")).scalar(), "on")
        with self.assertRaises(DBAPIError):
            with self.accounts.transaction(snapshot=True) as db:
                db.execute(text(f'CREATE TABLE "snapshot_forbidden_{uuid4().hex}"(id integer)'))

    def test_status_snapshot_does_not_wait_for_a_writer_and_does_not_mix_commits(self):
        with self.engine.connect() as db:
            if "wasm" in db.execute(text("SELECT version()")).scalar().lower():
                self.skipTest("PGlite multiplexes one connection; native PostgreSQL required for row-lock concurrency")
        table = f'"snapshot_lock_{uuid4().hex}"'
        with self.engine.begin() as db:
            db.execute(text(f"CREATE TABLE {table}(id integer PRIMARY KEY,value integer)"))
            db.execute(text(f"INSERT INTO {table} VALUES(1,10)"))
        try:
            with self.engine.connect() as writer:
                transaction = writer.begin()
                try:
                    writer.execute(text(f"UPDATE {table} SET value=20 WHERE id=1"))
                    started = time.monotonic()
                    with self.accounts.transaction(snapshot=True) as reader:
                        reader.execute(text("SET LOCAL statement_timeout='1000ms'"))
                        self.assertEqual(reader.execute(text(f"SELECT value FROM {table} WHERE id=1")).scalar(), 10)
                        self.assertLess(time.monotonic() - started, 1)
                        transaction.commit()
                        self.assertEqual(reader.execute(text(f"SELECT value FROM {table} WHERE id=1")).scalar(), 10)
                    with self.accounts.transaction(snapshot=True) as reader:
                        self.assertEqual(reader.execute(text(f"SELECT value FROM {table} WHERE id=1")).scalar(), 20)
                finally:
                    if transaction.is_active:
                        transaction.rollback()
        finally:
            with self.engine.begin() as db:
                db.execute(text(f"DROP TABLE {table}"))
