from __future__ import annotations

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from src.accounts import Account, decrypt_refresh_token
from src.config import env
from src.oauth_flow import SCOPES


def build_gmail_service_for_account(account: Account):
    """Reconstruct a Gmail API service from an account's stored (encrypted)
    refresh token. Refreshes eagerly so a dead/revoked/expired token surfaces
    immediately as google.auth.exceptions.RefreshError, right where the
    caller can mark the account needs_reauth and move on to the next one."""
    creds = Credentials(
        None,
        refresh_token=decrypt_refresh_token(account),
        client_id=env("GOOGLE_CLIENT_ID"),
        client_secret=env("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)
