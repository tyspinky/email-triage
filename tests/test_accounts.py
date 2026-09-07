import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet

from src.accounts import (
    decrypt_refresh_token,
    disconnect_account,
    get_account,
    get_account_by_email,
    get_summary_cache,
    list_active_accounts,
    mark_error,
    mark_needs_reauth,
    mark_synced,
    save_summary_cache,
    upsert_account,
)
from src.db import get_connection

TEST_KEY = Fernet.generate_key().decode()


class AccountsTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = get_connection()
        self._created_ids: list[int] = []
        self._env_patch = patch("src.crypto.env", return_value=TEST_KEY)
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        if self._created_ids:
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM accounts WHERE id = ANY(%s)", (self._created_ids,))
            self.conn.commit()
        self.conn.close()

    def upsert(self, email: str, token: str = "refresh-token-value"):
        account = upsert_account(self.conn, google_email=email, refresh_token=token)
        self._created_ids.append(account.id)
        return account


class TestUpsertAccount(AccountsTestCase):
    def test_creates_a_new_account_as_active(self):
        account = self.upsert("new-user@example.com")
        self.assertEqual(account.google_email, "new-user@example.com")
        self.assertEqual(account.status, "active")

    def test_stores_the_refresh_token_encrypted_not_plaintext(self):
        account = self.upsert("encrypted-check@example.com", token="super-secret-refresh-token")
        self.assertNotIn("super-secret-refresh-token", account.encrypted_refresh_token)

    def test_decrypts_back_to_the_original_token(self):
        account = self.upsert("roundtrip@example.com", token="original-token-value")
        self.assertEqual(decrypt_refresh_token(account), "original-token-value")

    def test_second_sign_in_updates_token_and_reactivates(self):
        first = self.upsert("repeat-signin@example.com", token="old-token")
        mark_needs_reauth(self.conn, first.id, "expired")

        second = self.upsert("repeat-signin@example.com", token="new-token")
        self.assertEqual(second.id, first.id)  # same account, not a duplicate
        self.assertEqual(second.status, "active")
        self.assertEqual(decrypt_refresh_token(second), "new-token")

    def test_second_sign_in_clears_previous_error(self):
        first = self.upsert("clears-error@example.com")
        mark_error(self.conn, first.id, "some transient failure")

        second = self.upsert("clears-error@example.com")
        self.assertIsNone(second.last_error)


class TestGetAccount(AccountsTestCase):
    def test_get_account_by_id(self):
        created = self.upsert("by-id@example.com")
        fetched = get_account(self.conn, created.id)
        self.assertEqual(fetched.google_email, "by-id@example.com")

    def test_get_account_by_id_returns_none_when_missing(self):
        self.assertIsNone(get_account(self.conn, 9_999_999))

    def test_get_account_by_email(self):
        self.upsert("by-email@example.com")
        fetched = get_account_by_email(self.conn, "by-email@example.com")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.google_email, "by-email@example.com")

    def test_get_account_by_email_returns_none_when_missing(self):
        self.assertIsNone(get_account_by_email(self.conn, "does-not-exist@example.com"))


class TestListActiveAccounts(AccountsTestCase):
    def test_only_returns_active_accounts(self):
        active = self.upsert("active@example.com")
        needs_reauth = self.upsert("needs-reauth@example.com")
        mark_needs_reauth(self.conn, needs_reauth.id, "expired")
        disabled = self.upsert("disabled@example.com")
        disconnect_account(self.conn, disabled.id)

        active_ids = {a.id for a in list_active_accounts(self.conn)}
        self.assertIn(active.id, active_ids)
        self.assertNotIn(needs_reauth.id, active_ids)
        self.assertNotIn(disabled.id, active_ids)


class TestStatusTransitions(AccountsTestCase):
    def test_mark_needs_reauth_sets_status_and_error(self):
        account = self.upsert("reauth-test@example.com")
        mark_needs_reauth(self.conn, account.id, "invalid_grant: token expired")
        refreshed = get_account(self.conn, account.id)
        self.assertEqual(refreshed.status, "needs_reauth")
        self.assertIn("invalid_grant", refreshed.last_error)

    def test_mark_error_keeps_account_active(self):
        account = self.upsert("transient-error@example.com")
        mark_error(self.conn, account.id, "temporary Gmail API hiccup")
        refreshed = get_account(self.conn, account.id)
        self.assertEqual(refreshed.status, "active")
        self.assertIn("hiccup", refreshed.last_error)

    def test_mark_synced_clears_error_and_sets_timestamp(self):
        account = self.upsert("sync-test@example.com")
        mark_error(self.conn, account.id, "some error")
        mark_synced(self.conn, account.id)
        refreshed = get_account(self.conn, account.id)
        self.assertIsNone(refreshed.last_error)
        self.assertIsNotNone(refreshed.last_synced_at)

    def test_disconnect_disables_and_wipes_token(self):
        account = self.upsert("disconnect-test@example.com")
        disconnect_account(self.conn, account.id)
        refreshed = get_account(self.conn, account.id)
        self.assertEqual(refreshed.status, "disabled")
        self.assertEqual(refreshed.encrypted_refresh_token, "")


class TestSummaryCache(AccountsTestCase):
    def test_returns_none_when_no_cache_saved_yet(self):
        account = self.upsert("no-cache@example.com")
        self.assertIsNone(get_summary_cache(self.conn, account.id))

    def test_round_trips_ids_and_text(self):
        account = self.upsert("cache-roundtrip@example.com")
        save_summary_cache(self.conn, account.id, ["m1", "m2"], "Here's your summary.")
        cached = get_summary_cache(self.conn, account.id)
        self.assertEqual(cached, (["m1", "m2"], "Here's your summary."))

    def test_overwrites_previous_cache(self):
        account = self.upsert("cache-overwrite@example.com")
        save_summary_cache(self.conn, account.id, ["m1"], "First")
        save_summary_cache(self.conn, account.id, ["m1", "m2"], "Second")
        cached = get_summary_cache(self.conn, account.id)
        self.assertEqual(cached, (["m1", "m2"], "Second"))


if __name__ == "__main__":
    unittest.main()
