from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg2.extensions

from src.crypto import decrypt_token, encrypt_token


@dataclass
class Account:
    id: int
    google_email: str
    status: str  # 'active' | 'needs_reauth' | 'disabled'
    encrypted_refresh_token: str
    last_error: str | None = None
    last_synced_at: datetime | None = None


def _row_to_account(row) -> Account:
    return Account(
        id=row[0],
        google_email=row[1],
        encrypted_refresh_token=row[2],
        status=row[3],
        last_error=row[4],
        last_synced_at=row[5],
    )


_SELECT_COLUMNS = "id, google_email, encrypted_refresh_token, status, last_error, last_synced_at"


def get_account(conn: psycopg2.extensions.connection, account_id: int) -> Account | None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_SELECT_COLUMNS} FROM accounts WHERE id = %s", (account_id,))
        row = cur.fetchone()
    return _row_to_account(row) if row else None


def get_account_by_email(conn: psycopg2.extensions.connection, google_email: str) -> Account | None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_SELECT_COLUMNS} FROM accounts WHERE google_email = %s", (google_email,))
        row = cur.fetchone()
    return _row_to_account(row) if row else None


def upsert_account(conn: psycopg2.extensions.connection, *, google_email: str, refresh_token: str) -> Account:
    """Create the account on first sign-in, or update its stored token (and
    reactivate it) on a subsequent sign-in — e.g. after a Testing-mode 7-day
    expiry forced a re-auth."""
    encrypted = encrypt_token(refresh_token)
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO accounts (google_email, encrypted_refresh_token, status, updated_at)
                VALUES (%s, %s, 'active', now())
                ON CONFLICT (google_email) DO UPDATE
                    SET encrypted_refresh_token = EXCLUDED.encrypted_refresh_token,
                        status = 'active',
                        last_error = NULL,
                        updated_at = now()
                RETURNING {_SELECT_COLUMNS}""",
            (google_email, encrypted),
        )
        row = cur.fetchone()
    conn.commit()
    return _row_to_account(row)


def list_active_accounts(conn: psycopg2.extensions.connection) -> list[Account]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_SELECT_COLUMNS} FROM accounts WHERE status = 'active'")
        rows = cur.fetchall()
    return [_row_to_account(row) for row in rows]


def mark_needs_reauth(conn: psycopg2.extensions.connection, account_id: int, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE accounts SET status = 'needs_reauth', last_error = %s, updated_at = now() WHERE id = %s",
            (error, account_id),
        )
    conn.commit()


def mark_synced(conn: psycopg2.extensions.connection, account_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE accounts SET last_synced_at = now(), last_error = NULL, updated_at = now() WHERE id = %s",
            (account_id,),
        )
    conn.commit()


def mark_error(conn: psycopg2.extensions.connection, account_id: int, error: str) -> None:
    """Transient failure (Gmail API hiccup, etc.) — logged, but the account
    stays 'active' and gets retried next cycle, unlike mark_needs_reauth."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE accounts SET last_error = %s, updated_at = now() WHERE id = %s",
            (error, account_id),
        )
    conn.commit()


def disconnect_account(conn: psycopg2.extensions.connection, account_id: int) -> None:
    """'Delete my data' action: wipe the stored token and disable the
    account. Classification history is kept (not a full data-delete) unless
    the caller also removes the account row entirely."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE accounts SET status = 'disabled', encrypted_refresh_token = '', updated_at = now() WHERE id = %s",
            (account_id,),
        )
    conn.commit()


def get_summary_cache(conn: psycopg2.extensions.connection, account_id: int) -> tuple[list[str], str] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT summary_cache_ids, summary_cache_text FROM accounts WHERE id = %s", (account_id,))
        row = cur.fetchone()
    if not row or row[0] is None or row[1] is None:
        return None
    return json.loads(row[0]), row[1]


def save_summary_cache(conn: psycopg2.extensions.connection, account_id: int, ids: list[str], text: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE accounts SET summary_cache_ids = %s, summary_cache_text = %s, updated_at = now() WHERE id = %s",
            (json.dumps(ids), text, account_id),
        )
    conn.commit()


def decrypt_refresh_token(account: Account) -> str:
    return decrypt_token(account.encrypted_refresh_token)
