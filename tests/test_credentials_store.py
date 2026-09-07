import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.auth.exceptions import RefreshError

from src.accounts import Account
from src.credentials_store import build_gmail_service_for_account


def make_account(encrypted_refresh_token="ciphertext") -> Account:
    return Account(
        id=1,
        google_email="test@example.com",
        encrypted_refresh_token=encrypted_refresh_token,
        status="active",
    )


class TestBuildGmailServiceForAccount(unittest.TestCase):
    def test_decrypts_token_and_builds_service(self):
        account = make_account()
        fake_service = object()

        with patch("src.credentials_store.decrypt_refresh_token", return_value="plaintext-refresh-token") as mock_decrypt, \
             patch("src.credentials_store.env", side_effect=lambda name: f"fake-{name}"), \
             patch("src.credentials_store.Credentials") as mock_creds_cls, \
             patch("src.credentials_store.build", return_value=fake_service) as mock_build:
            mock_creds_instance = MagicMock()
            mock_creds_cls.return_value = mock_creds_instance

            result = build_gmail_service_for_account(account)

        mock_decrypt.assert_called_once_with(account)
        mock_creds_cls.assert_called_once()
        _, kwargs = mock_creds_cls.call_args
        self.assertEqual(kwargs["refresh_token"], "plaintext-refresh-token")
        self.assertEqual(kwargs["client_id"], "fake-GOOGLE_CLIENT_ID")
        self.assertEqual(kwargs["client_secret"], "fake-GOOGLE_CLIENT_SECRET")
        mock_creds_instance.refresh.assert_called_once()
        mock_build.assert_called_once_with("gmail", "v1", credentials=mock_creds_instance)
        self.assertIs(result, fake_service)

    def test_refresh_error_propagates_not_swallowed(self):
        # The caller (the multi-account scheduler loop) relies on this
        # bubbling up so it can mark the account needs_reauth and move on.
        account = make_account()

        with patch("src.credentials_store.decrypt_refresh_token", return_value="dead-token"), \
             patch("src.credentials_store.env", return_value="fake-value"), \
             patch("src.credentials_store.Credentials") as mock_creds_cls:
            mock_creds_instance = MagicMock()
            mock_creds_instance.refresh.side_effect = RefreshError("invalid_grant: Token has been expired or revoked.")
            mock_creds_cls.return_value = mock_creds_instance

            with self.assertRaises(RefreshError):
                build_gmail_service_for_account(account)

    def test_refreshes_eagerly_before_building_service(self):
        # Order matters: refresh() must happen before build(), so a dead
        # token is caught here rather than surfacing later as an obscure
        # Gmail API 401 deep inside the triage pipeline.
        account = make_account()
        call_order = []

        with patch("src.credentials_store.decrypt_refresh_token", return_value="token"), \
             patch("src.credentials_store.env", return_value="fake-value"), \
             patch("src.credentials_store.Credentials") as mock_creds_cls, \
             patch("src.credentials_store.build", side_effect=lambda *a, **kw: call_order.append("build")):
            mock_creds_instance = MagicMock()
            mock_creds_instance.refresh.side_effect = lambda *a: call_order.append("refresh")
            mock_creds_cls.return_value = mock_creds_instance

            build_gmail_service_for_account(account)

        self.assertEqual(call_order, ["refresh", "build"])


if __name__ == "__main__":
    unittest.main()
