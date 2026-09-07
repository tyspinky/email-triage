"""Cross-account isolation is the single most important property in this
whole multi-tenant rearchitecture: a logged-in user must never be able to see
another account's data, however they try. Uses Flask's test client against
the real app + a real local Postgres, with fake Gmail/LLM calls only where
the network would otherwise be hit."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module
from src.config import Config
from src.db import get_connection, log_classification


def make_config():
    return Config(
        folders=["Clients", "Internal", "Finance", "Vendors", "Newsletters", "Other"],
        default_folder="Other",
        urgency_tiers=["Urgent", "Today", "ThisWeek", "NoAction"],
        default_urgency="NoAction",
        gmail_query="in:inbox",
        label_prefix="Triage",
        remove_from_inbox=False,
        llm_model="claude-haiku-4-5-20251001",
        llm_max_body_chars=300,
        llm_enabled=True,
        rules=[],
        folder_colors={},
        urgency_colors={},
    )


class WebAuthTestCase(unittest.TestCase):
    def setUp(self):
        app_module.app.testing = True
        self.client = app_module.app.test_client()
        self.conn = get_connection()
        self._account_ids: list[int] = []

    def tearDown(self):
        if self._account_ids:
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM accounts WHERE id = ANY(%s)", (self._account_ids,))
            self.conn.commit()
        self.conn.close()

    def make_account(self, email: str, status: str = "active") -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO accounts (google_email, encrypted_refresh_token, status) VALUES (%s, %s, %s) RETURNING id",
                (email, "placeholder", status),
            )
            account_id = cur.fetchone()[0]
        self.conn.commit()
        self._account_ids.append(account_id)
        return account_id

    def log_in_as(self, account_id: int) -> None:
        with self.client.session_transaction() as sess:
            sess["account_id"] = account_id

    def log(self, account_id: int, message_id: str, subject: str, urgency="Urgent", folder="Finance"):
        log_classification(
            self.conn,
            account_id=account_id,
            message_id=message_id,
            thread_id=f"t-{message_id}",
            sender="a@b.com",
            subject=subject,
            folder=folder,
            urgency=urgency,
            method="rule",
            confidence=1.0,
            archived=False,
        )


class TestLoginRequired(WebAuthTestCase):
    def test_dashboard_redirects_to_login_when_not_authenticated(self):
        resp = self.client.get("/dashboard")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_search_redirects_to_login_when_not_authenticated(self):
        resp = self.client.get("/search?q=test")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_disabled_account_is_treated_as_logged_out(self):
        account_id = self.make_account("disabled@example.com", status="disabled")
        self.log_in_as(account_id)
        resp = self.client.get("/dashboard")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_needs_reauth_account_redirected_to_login_from_dashboard(self):
        account_id = self.make_account("needs-reauth@example.com", status="needs_reauth")
        self.log_in_as(account_id)
        resp = self.client.get("/dashboard")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_index_redirects_logged_in_user_to_dashboard(self):
        account_id = self.make_account("index-redirect@example.com")
        self.log_in_as(account_id)
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard", resp.headers["Location"])

    def test_logout_clears_session(self):
        account_id = self.make_account("logout-test@example.com")
        self.log_in_as(account_id)
        self.client.get("/logout")
        resp = self.client.get("/dashboard")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])


class TestCrossAccountIsolation(WebAuthTestCase):
    def test_dashboard_shows_only_the_logged_in_accounts_data(self):
        account_a = self.make_account("account-a@example.com")
        account_b = self.make_account("account-b@example.com")
        self.log(account_a, "m1", "Account A's private urgent matter")
        self.log(account_b, "m2", "Account B's private urgent matter")

        self.log_in_as(account_a)
        with patch("src.dashboard.generate_llm_summary", return_value="Summary"):
            resp = self.client.get("/dashboard")

        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("Account A's private urgent matter", body)
        self.assertNotIn("Account B's private urgent matter", body)

    def test_query_param_tampering_cannot_access_another_account(self):
        # There's no account-id request parameter in this design at all —
        # this confirms attempting one is simply ignored, not honored.
        account_a = self.make_account("tamper-a@example.com")
        account_b = self.make_account("tamper-b@example.com")
        self.log(account_a, "m1", "A's data")
        self.log(account_b, "m2", "B's data")

        self.log_in_as(account_a)
        with patch("src.dashboard.generate_llm_summary", return_value="Summary"):
            resp = self.client.get(f"/dashboard?account_id={account_b}")

        body = resp.get_data(as_text=True)
        self.assertIn("A's data", body)
        self.assertNotIn("B's data", body)

    def test_search_only_ever_searches_the_logged_in_accounts_gmail(self):
        account_a = self.make_account("search-isolation-a@example.com")
        self.log_in_as(account_a)

        captured_accounts = []

        def fake_build_service(account):
            captured_accounts.append(account.id)
            return object()

        with patch("app.build_gmail_service_for_account", side_effect=fake_build_service), patch(
            "app.run_search", return_value=[]
        ):
            self.client.get("/search?q=business")

        self.assertEqual(captured_accounts, [account_a])

    def test_two_logged_in_sessions_never_cross_contaminate(self):
        account_a = self.make_account("session-a@example.com")
        account_b = self.make_account("session-b@example.com")
        self.log(account_a, "m1", "A only")
        self.log(account_b, "m2", "B only")

        client_a = app_module.app.test_client()
        client_b = app_module.app.test_client()
        with client_a.session_transaction() as sess:
            sess["account_id"] = account_a
        with client_b.session_transaction() as sess:
            sess["account_id"] = account_b

        with patch("src.dashboard.generate_llm_summary", return_value="Summary"):
            resp_a = client_a.get("/dashboard")
            resp_b = client_b.get("/dashboard")

        body_a = resp_a.get_data(as_text=True)
        body_b = resp_b.get_data(as_text=True)
        self.assertIn("A only", body_a)
        self.assertNotIn("B only", body_a)
        self.assertIn("B only", body_b)
        self.assertNotIn("A only", body_b)


class TestOAuthCallback(WebAuthTestCase):
    def test_missing_state_is_rejected(self):
        resp = self.client.get("/oauth/callback?code=fake&state=whatever")
        self.assertEqual(resp.status_code, 400)

    def test_mismatched_state_is_rejected(self):
        with self.client.session_transaction() as sess:
            sess["oauth_state"] = "expected-value"
        resp = self.client.get("/oauth/callback?code=fake&state=wrong-value")
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
