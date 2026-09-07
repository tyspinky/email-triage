from __future__ import annotations

import os

from google_auth_oauthlib.flow import Flow

from src.config import env

# gmail.modify covers reading messages and adding/removing labels, but NOT
# sending mail or permanently deleting anything — the minimum this tool needs.
# It also covers users().getProfile(), so no separate userinfo/openid scope
# is needed just to learn the signed-in account's email address.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# google-auth-oauthlib can raise "Scope has changed" if Google's response
# includes scopes in a different order/format than requested — harmless, but
# noisy without this.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


def build_flow() -> Flow:
    client_config = {
        "web": {
            "client_id": env("GOOGLE_CLIENT_ID"),
            "client_secret": env("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = env("GOOGLE_OAUTH_REDIRECT_URI")
    return flow
