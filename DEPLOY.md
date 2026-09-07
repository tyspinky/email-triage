# Deploying to Render

`render.yaml` declares the whole architecture: a web service (the Flask app),
a background worker (the 5-minute triage scheduler), and a managed Postgres
database. Render reads this file automatically as a "Blueprint."

## 1. Push this repo somewhere Render can see it

Render deploys from a Git remote (GitHub/GitLab/Bitbucket). If this isn't a
git repo yet:

```bash
git init
git add .
git commit -m "Initial commit"
```

Then push it to a new GitHub repo and connect that in the next step.

## 2. Create the Blueprint

1. Go to https://dashboard.render.com/blueprints
2. **New Blueprint Instance** → connect your repo → Render detects
   `render.yaml` and shows the three services it'll create.
3. Click through to create them. The web and worker services will fail to
   start on the first deploy — that's expected, the secret env vars below
   aren't set yet.

## 3. Set the secret environment variables

In the Render dashboard, for **both** `email-triage-web` and
`email-triage-scheduler`, set (these are the ones marked `sync: false` in
`render.yaml`, so Render doesn't manage them for you):

- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — from your Google Cloud OAuth
  Web application client (see SETUP.md step 3).
- `TOKEN_ENCRYPTION_KEYS` — generate once locally, use the same value on both
  services (they both need to decrypt the same tokens):
  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- `ANTHROPIC_API_KEY`

On `email-triage-web` only, also set:
- `FLASK_SECRET_KEY` — generate with `python -c "import secrets; print(secrets.token_hex(32))"`

`DATABASE_URL` is wired automatically by the blueprint (`fromDatabase`) — you
don't set it by hand.

## 4. Register the production redirect URI with Google

Once the web service has a URL (Render assigns one like
`https://email-triage-web.onrender.com`), go back to Google Cloud Console →
your OAuth client → **Authorized redirect URIs** → add
`https://email-triage-web.onrender.com/oauth/callback`. You can keep the
`localhost:5000` one registered alongside it for local dev.

Also update `GOOGLE_OAUTH_REDIRECT_URI` on the web service to match (and the
`value:` in `render.yaml` if your actual Render URL differs from the
placeholder there).

## 5. Redeploy and test

Trigger a manual deploy (or push a commit) once the env vars are set. Then:

1. Add a pilot's Gmail address as a Google Cloud Console test user (SETUP.md
   step 2.5) — this is still a manual, per-pilot step while in Testing mode.
2. Visit your Render URL, sign in as that test user.
3. Within one scheduler cycle (≤5 min), their dashboard should show their
   real triaged mail.
4. Check the `email-triage-scheduler` service's logs in Render to confirm
   the cycle ran and see any per-account errors.

## Ongoing operations

- **Adding a new pilot**: add their Gmail to the Console test-user list, send
  them the app URL. No deploy needed.
- **A pilot's access breaks**: check `accounts.status` in Postgres (Render's
  dashboard has a built-in query console) — `needs_reauth` means their
  7-day Testing-mode token expired; they just need to sign in again.
- **Logs**: both services stream logs in the Render dashboard. The scheduler
  logs one line per account per cycle plus any errors.
