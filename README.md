# Inbox Triage

A small self-serve web app: sign in with Google and it automatically sorts
your incoming Gmail into labeled folders and ranks each message by urgency
(`Urgent` / `Today` / `ThisWeek` / `NoAction`), so you can see what needs
attention without reading your whole inbox. Includes a dashboard (todo list +
AI summary + browse-by-category view) and a natural-language search box.

- **Rules-first**: sender domain/address, sender local-part prefix (e.g.
  `no-reply@...`), subject keywords, and List-Unsubscribe header presence are
  matched with zero API calls.
- Only messages no rule matches get sent to Claude Haiku for classification,
  using a trimmed snippet (subject + ~300 chars of plain text, quoted replies
  and signatures stripped).
- Every classification (rule-based or LLM) is logged to Postgres for later
  review, scoped per signed-in account.
- **Multi-tenant**: any Google account you've added as a test user (see
  SETUP.md) can sign in independently — each account's mail, labels, and
  audit log are fully isolated from every other's.

## Setup

First time running this at all: see [SETUP.md](SETUP.md) (Google Cloud OAuth
client, local Postgres, `.env`). Deploying so real pilot users can sign in:
see [DEPLOY.md](DEPLOY.md) (Render).

## Configure folders and rules

Edit [config/config.yaml](config/config.yaml):

- `folders`: the label names you want (defaults: Clients, Internal, Finance,
  Vendors, Security, Newsletters, Other).
- `rules`: sender/subject patterns mapped to a folder + urgency tier. Several
  placeholder examples are included (`example-client.com`,
  `yourcompany.com`) — edit or remove them for your real categories.
- `gmail.remove_from_inbox`: `false` by default, so labeled messages stay
  visible in the inbox. Flip to `true` to archive triaged mail automatically.

Rules are evaluated top-to-bottom; the first fully-matching rule wins. See
the comments in `config.yaml` for the condition types available.

**Current limitation**: one shared `config.yaml` across every signed-in
account — no per-account rule customization yet. Fine for a small pilot,
worth revisiting if this grows.

## Running it

Run the test suite (needs a local Postgres — see SETUP.md):

```bash
.venv/bin/python -m unittest discover -s tests
```

Run the web app locally:

```bash
.venv/bin/python -m flask --app app run --port 5000
```

Trigger a triage cycle manually (in production this runs automatically every
5 minutes via `scheduler.py`):

```bash
.venv/bin/python run.py --dry-run --limit 5   # preview only, no labels touched
.venv/bin/python run.py                        # for real, every active account
```

`run.py` processes **every active account** in one pass — there's no
single-account concept from the CLI anymore, since each account authenticates
via its own web login.

### Where things show up in Gmail

Labels are created nested under `Triage/`, per account:

- `Triage/<Folder>` — e.g. `Triage/Clients`, `Triage/Newsletters`
- `Triage/Urgency/<Tier>` — e.g. `Triage/Urgency/Urgent`
- `Triage/Processed` — internal marker so messages aren't reclassified

## The dashboard and search

Once signed in, `/dashboard` shows:
- A prioritized todo list with an AI-generated summary (cached — regenerates
  only when the actionable set actually changes, to keep Claude API costs
  down)
- Every folder as a collapsible category, even empty ones
- A search box: type a natural-language request (e.g. "emails about
  business"), and it translates that into Gmail search keywords, fetches
  candidates, then has Claude pick and explain the most relevant ones

## Reviewing accuracy

Every decision is logged to Postgres, scoped per account:

```bash
psql email_triage_dev -c "select sender, subject, folder, urgency, method, confidence from classifications where account_id = 1 order by created_at desc limit 20;"
```

`method` is either the name of the rule that matched, `llm`, `llm_error` (the
LLM call failed, safe default used), or `default`. Use this to spot-check
rules/LLM accuracy and tighten `config.yaml` — there's no auto-correction
loop yet (deliberately out of scope for now).

## Account status

Each account in the `accounts` table has a `status`:
- `active` — triaging normally
- `needs_reauth` — their Gmail OAuth token expired (Testing-mode tokens
  expire after 7 days — see SETUP.md) or was revoked; they need to sign in
  again
- `disabled` — they disconnected their account; their stored token is wiped,
  classification history is kept unless the row is deleted

## Debugging one account's search from a terminal

```bash
.venv/bin/python search.py their-email@example.com "find their business emails"
```

Looks the account up by email and runs a real search with its stored
credentials — useful for debugging without going through the browser.
