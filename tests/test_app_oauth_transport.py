"""Regression test for a real bug hit during local dev: oauthlib refuses to
process an OAuth response over plain http://, which always breaks a local
dev server (no HTTPS on localhost) unless OAUTHLIB_INSECURE_TRANSPORT is set.
That relaxation must only ever apply to a localhost redirect URI — never to
a production https:// one, or it would silently weaken a real security check.

Uses importlib.reload(), not del sys.modules["app"] + re-import: other test
files do `import app as app_module` and hold that module object directly.
Deleting and re-importing creates a *new* object, orphaning their reference
(and any string-based unittest.mock.patch("app.xxx"), which resolves via
sys.modules at patch time) — reload() mutates the existing object in place
instead, so every other test file's reference stays valid."""

import importlib
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module


class TestOauthInsecureTransportFlag(unittest.TestCase):
    def setUp(self):
        self._orig_redirect_uri = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI")
        self._orig_flag = os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)

    def tearDown(self):
        if self._orig_redirect_uri is None:
            os.environ.pop("GOOGLE_OAUTH_REDIRECT_URI", None)
        else:
            os.environ["GOOGLE_OAUTH_REDIRECT_URI"] = self._orig_redirect_uri
        if self._orig_flag is None:
            os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)
        else:
            os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = self._orig_flag
        # Restore the shared module object to its original-env state so
        # every other test file's `import app as app_module` reference
        # (bound once, at discovery time) stays consistent afterward.
        importlib.reload(app_module)

    def test_localhost_redirect_uri_relaxes_transport_check(self):
        os.environ["GOOGLE_OAUTH_REDIRECT_URI"] = "http://localhost:8000/oauth/callback"
        importlib.reload(app_module)
        self.assertEqual(os.environ.get("OAUTHLIB_INSECURE_TRANSPORT"), "1")

    def test_127_0_0_1_redirect_uri_relaxes_transport_check(self):
        os.environ["GOOGLE_OAUTH_REDIRECT_URI"] = "http://127.0.0.1:8000/oauth/callback"
        importlib.reload(app_module)
        self.assertEqual(os.environ.get("OAUTHLIB_INSECURE_TRANSPORT"), "1")

    def test_production_https_redirect_uri_does_not_relax_transport_check(self):
        os.environ["GOOGLE_OAUTH_REDIRECT_URI"] = "https://email-triage-web.onrender.com/oauth/callback"
        importlib.reload(app_module)
        self.assertIsNone(os.environ.get("OAUTHLIB_INSECURE_TRANSPORT"))


if __name__ == "__main__":
    unittest.main()
