"""Tests against a real local Postgres (not SQLite) — the project deliberately
dropped an ORM abstraction layer, so these tests need `DATABASE_URL` pointing
at a real dev Postgres instance (see .env / README for local setup)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import get_connection, log_classification, run_migrations


class PostgresTestCase(unittest.TestCase):
    """Base class: log_classification()/account creation commit internally
    (real production behavior), so cleanup can't rely on a rollback — instead
    track every account created via self.make_account() and delete them (and
    their cascaded classifications) in tearDown."""

    def setUp(self):
        self.conn = get_connection()
        self._created_account_ids: list[int] = []

    def tearDown(self):
        if self._created_account_ids:
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM accounts WHERE id = ANY(%s)", (self._created_account_ids,))
            self.conn.commit()
        self.conn.close()

    def make_account(self, google_email: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO accounts (google_email, encrypted_refresh_token) VALUES (%s, %s) RETURNING id",
                (google_email, "encrypted-placeholder"),
            )
            account_id = cur.fetchone()[0]
        self.conn.commit()
        self._created_account_ids.append(account_id)
        return account_id


class TestMigrations(unittest.TestCase):
    def test_migrations_table_and_core_tables_exist(self):
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
                )
                tables = {row[0] for row in cur.fetchall()}
            self.assertIn("accounts", tables)
            self.assertIn("classifications", tables)
            self.assertIn("schema_migrations", tables)
        finally:
            conn.close()

    def test_running_migrations_twice_is_a_no_op(self):
        conn = get_connection()
        try:
            applied_again = run_migrations(conn)
            self.assertEqual(applied_again, [])
        finally:
            conn.close()


class TestLogClassification(PostgresTestCase):
    def test_stores_and_retrieves_a_row_scoped_to_account(self):
        account_id = self.make_account("test-log-classification@example.com")
        log_classification(
            self.conn,
            account_id=account_id,
            message_id="m1",
            thread_id="t1",
            sender="a@b.com",
            subject="Test subject",
            folder="Finance",
            urgency="Today",
            method="rule",
            confidence=1.0,
            archived=False,
            snippet="A short preview.",
        )
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT subject, folder, urgency, snippet, account_id FROM classifications WHERE message_id = %s",
                ("m1",),
            )
            row = cur.fetchone()
        self.assertEqual(row, ("Test subject", "Finance", "Today", "A short preview.", account_id))

    def test_snippet_defaults_to_empty_string(self):
        account_id = self.make_account("test-default-snippet@example.com")
        log_classification(
            self.conn,
            account_id=account_id,
            message_id="m2",
            thread_id="t2",
            sender="a@b.com",
            subject="Test",
            folder="Other",
            urgency="NoAction",
            method="rule",
            confidence=1.0,
            archived=False,
        )
        with self.conn.cursor() as cur:
            cur.execute("SELECT snippet FROM classifications WHERE message_id = %s", ("m2",))
            self.assertEqual(cur.fetchone()[0], "")

    def test_two_accounts_can_log_the_same_message_id_independently(self):
        # Gmail message ids aren't globally unique across accounts, so this
        # must not collide — account_id is part of what distinguishes rows.
        account_1 = self.make_account("acct1@example.com")
        account_2 = self.make_account("acct2@example.com")
        for account_id in (account_1, account_2):
            log_classification(
                self.conn,
                account_id=account_id,
                message_id="shared-id",
                thread_id="t",
                sender="a@b.com",
                subject="Test",
                folder="Other",
                urgency="NoAction",
                method="rule",
                confidence=1.0,
                archived=False,
            )
        with self.conn.cursor() as cur:
            cur.execute("SELECT account_id FROM classifications WHERE message_id = %s ORDER BY account_id", ("shared-id",))
            rows = [r[0] for r in cur.fetchall()]
        self.assertEqual(rows, sorted([account_1, account_2]))

    def test_deleting_an_account_cascades_to_its_classifications(self):
        account_id = self.make_account("cascade-test@example.com")
        log_classification(
            self.conn,
            account_id=account_id,
            message_id="m-cascade",
            thread_id="t",
            sender="a@b.com",
            subject="Test",
            folder="Other",
            urgency="NoAction",
            method="rule",
            confidence=1.0,
            archived=False,
        )
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
            cur.execute("SELECT COUNT(*) FROM classifications WHERE account_id = %s", (account_id,))
            self.assertEqual(cur.fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
