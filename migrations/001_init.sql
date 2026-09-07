-- accounts: one row per tenant (one Gmail account each)
CREATE TABLE IF NOT EXISTS accounts (
    id SERIAL PRIMARY KEY,
    google_email TEXT NOT NULL UNIQUE,
    google_sub TEXT UNIQUE,                 -- Google's stable subject id
    encrypted_refresh_token TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'needs_reauth' | 'disabled'
    last_error TEXT,
    last_synced_at TIMESTAMPTZ,
    summary_cache_ids TEXT,                 -- JSON array of sorted actionable message_ids
    summary_cache_text TEXT,                -- cached LLM summary for that exact id set
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- classifications: same shape as the old single-tenant SQLite table + account_id
CREATE TABLE IF NOT EXISTS classifications (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    message_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    subject TEXT NOT NULL,
    folder TEXT NOT NULL,
    urgency TEXT NOT NULL,
    method TEXT NOT NULL,
    confidence REAL NOT NULL,
    archived BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    snippet TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_classifications_account_message
    ON classifications(account_id, message_id);
CREATE INDEX IF NOT EXISTS idx_classifications_account_created_at
    ON classifications(account_id, created_at);
