import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.oauth_flow import SCOPES, build_flow


def fake_env(name, default=None):
    values = {
        "GOOGLE_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "test-client-secret",
        "GOOGLE_OAUTH_REDIRECT_URI": "https://example.com/oauth/callback",
    }
    return values.get(name, default)


class TestBuildFlow(unittest.TestCase):
    def test_uses_gmail_modify_scope_only(self):
        self.assertEqual(SCOPES, ["https://www.googleapis.com/auth/gmail.modify"])

    def test_sets_redirect_uri_from_env(self):
        with patch("src.oauth_flow.env", side_effect=fake_env):
            flow = build_flow()
        self.assertEqual(flow.redirect_uri, "https://example.com/oauth/callback")

    def test_uses_client_id_and_secret_from_env(self):
        with patch("src.oauth_flow.env", side_effect=fake_env):
            flow = build_flow()
        self.assertEqual(flow.client_config["client_id"], "test-client-id.apps.googleusercontent.com")
        self.assertEqual(flow.client_config["client_secret"], "test-client-secret")

    def test_flow_requests_gmail_modify_scope(self):
        with patch("src.oauth_flow.env", side_effect=fake_env):
            flow = build_flow()
        self.assertEqual(list(flow.oauth2session.scope), SCOPES)


if __name__ == "__main__":
    unittest.main()
