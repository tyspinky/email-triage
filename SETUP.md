# Setup: Google Cloud, Postgres, and running the app locally

This is now a small multi-tenant web app: anyone you add as a test user can
sign in with their own Google account and get their inbox triaged, without
you touching their Gmail credentials directly. This guide covers local dev
setup; see [DEPLOY.md](DEPLOY.md) for putting it on Render.

## 1. Google Cloud project

1. Go to https://console.cloud.google.com/projectcreate and create a project
   (e.g. `email-triage`).
2. Enable the Gmail API: https://console.cloud.google.com/apis/library/gmail.googleapis.com
   (with your project selected) → **Enable**.

## 2. OAuth consent screen

1. Go to https://console.cloud.google.com/apis/credentials/consent
2. User type: **External**, unless every pilot user is in the same Google
   Workspace organization as you — in that case **Internal** skips the
   test-user allowlist below entirely. For a personal-Gmail pilot, External
   is your only option.
3. Fill in App name, support email, developer contact email.
4. `gmail.modify` is a **restricted** scope. Google requires an **App
   homepage URL** and a **Privacy policy URL** before it will even save this
   screen. Point both at your running app — `https://<your-app>/` and
   `https://<your-app>/privacy` (the privacy page is already built in, see
   `app.py`). Locally, `http://localhost:5000/privacy` works for filling the
   field in even though Google can't actually reach it in dev.
5. **Test users**: add the Gmail address of every pilot you want to be able
   to sign in. This is manual and per-user — Google enforces a 100-user cap
   in Testing status, and only listed addresses can complete the OAuth flow
   at all. Anyone else sees Google's own `Error 403: access_denied` page.
6. **Leave publishing status as "Testing."** This is a deliberate tradeoff:
   Testing-mode refresh tokens expire after 7 days regardless of activity, so
   every pilot re-authenticates weekly — but it requires zero Google review.
   Moving to "In production" avoids the weekly re-auth but risks Google
   flagging/restricting a restricted-scope app serving external users without
   verification. See the project plan for the full reasoning.

## 3. OAuth client credentials

1. Go to https://console.cloud.google.com/apis/credentials
2. **Create Credentials** → **OAuth client ID**.
3. Application type: **Web application** (not Desktop app — this app runs as
   a server, not a local script hitting a loopback redirect).
4. **Authorized redirect URIs**: add `http://localhost:5000/oauth/callback`
   for local dev. Add your production URL's `/oauth/callback` too once
   deployed (see DEPLOY.md) — you can have both registered at once.
5. Click **Create**. Copy the **Client ID** and **Client secret** — you'll
   put these in `.env` next.

## 4. Local Postgres

```bash
brew install postgresql@16
brew services start postgresql@16
createdb email_triage_dev
```

## 5. Python environment

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:
- `ANTHROPIC_API_KEY` — from https://console.anthropic.com/settings/keys
- `DATABASE_URL` — `postgresql:///email_triage_dev` works as-is if you used
  the `createdb` command above with Homebrew's default trust-auth setup.
- `TOKEN_ENCRYPTION_KEYS` — generate with:
  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- `FLASK_SECRET_KEY` — generate with:
  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — from step 3.
- `GOOGLE_OAUTH_REDIRECT_URI` — leave as `http://localhost:5000/oauth/callback`
  for local dev.

The database schema is created automatically (migrations run on first
connection) — no manual step needed.

## 6. Run it

```bash
.venv/bin/python -m flask --app app run --port 5000
```

Visit http://localhost:5000/, click **Sign in with Google**, and sign in with
one of the Gmail addresses you added as a test user in step 2. If you see an
**"unverified app"** warning (expected, since this stays unverified in
Testing status): click **Advanced** → **Go to \<app name\> (unsafe)** —
"unsafe" here just means Google hasn't manually reviewed it, not that
anything is actually wrong.

After approving, you'll land on your dashboard. Trigger a triage run manually
with:

```bash
.venv/bin/python run.py --dry-run --limit 5
```

(drop `--dry-run` once you're happy with what it would do). In production
this runs automatically every 5 minutes via `scheduler.py` — see
[README.md](README.md) for day-to-day usage and [DEPLOY.md](DEPLOY.md) for
putting this on Render for real pilots.
