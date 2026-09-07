from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extensions

from src.config import PROJECT_ROOT, env

MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def _ensure_migrations_table(conn: psycopg2.extensions.connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version TEXT PRIMARY KEY,
                   applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
               )"""
        )
    conn.commit()


def run_migrations(conn: psycopg2.extensions.connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply any .sql files in migrations_dir not yet recorded in
    schema_migrations, in filename order. Returns the versions just applied.
    Safe to call on every process startup — a no-op once everything's applied."""
    _ensure_migrations_table(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations")
        applied = {row[0] for row in cur.fetchall()}

    newly_applied = []
    for path in sorted(migrations_dir.glob("*.sql")):
        version = path.stem
        if version in applied:
            continue
        with conn.cursor() as cur:
            cur.execute(path.read_text())
            cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
        conn.commit()
        newly_applied.append(version)
    return newly_applied


def get_connection() -> psycopg2.extensions.connection:
    """A plain, unpooled connection per caller. Deliberately not pooled: a
    connection pool created at import time doesn't survive gunicorn's
    pre-fork workers cleanly, and at this scale (a handful of tenants, one
    scheduler loop) a fresh connection per request/iteration is simpler and
    safe."""
    conn = psycopg2.connect(env("DATABASE_URL"))
    run_migrations(conn)
    return conn


def log_classification(
    conn: psycopg2.extensions.connection,
    *,
    account_id: int,
    message_id: str,
    thread_id: str,
    sender: str,
    subject: str,
    folder: str,
    urgency: str,
    method: str,
    confidence: float,
    archived: bool,
    snippet: str = "",
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO classifications
               (account_id, message_id, thread_id, sender, subject, folder, urgency, method, confidence, archived, created_at, snippet)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                account_id,
                message_id,
                thread_id,
                sender,
                subject,
                folder,
                urgency,
                method,
                confidence,
                archived,
                datetime.now(timezone.utc),
                snippet,
            ),
        )
    conn.commit()
