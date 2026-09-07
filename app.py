from __future__ import annotations

from datetime import timedelta

from flask import Flask, g, jsonify, redirect, request, session, url_for
from google.auth.exceptions import RefreshError
from werkzeug.middleware.proxy_fix import ProxyFix

from src.accounts import disconnect_account, upsert_account
from src.config import env, load_config
from src.credentials_store import build_gmail_service_for_account
from src.dashboard import render_dashboard_for_account
from src.oauth_flow import build_flow
from src.search import DEFAULT_RESULT_COUNT, run_search
from src.web_auth import close_db, current_account, get_db, login_required

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = env("FLASK_SECRET_KEY")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    # Must be "Lax", not "Strict": Google's redirect back to /oauth/callback
    # is a top-level cross-site navigation. "Strict" would silently drop the
    # session cookie on that redirect, so the CSRF state check below would
    # always see a missing state and reject every real login.
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)
app.teardown_appcontext(close_db)


PRIVACY_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>Privacy Policy — Inbox Triage</title></head><body style="font-family:sans-serif;max-width:640px;margin:3rem auto;line-height:1.6">
<h1>Privacy Policy</h1>
<p>Inbox Triage connects to your Gmail account (via Google's OAuth, scope
<code>gmail.modify</code>) to read incoming mail, classify it, and apply
labels. Here's what that means for your data:</p>
<ul>
<li><strong>What's stored:</strong> a classification record per email (sender,
subject, folder, urgency, a short body preview) and an encrypted copy of your
Gmail refresh token, used only to check your inbox on your behalf.</li>
<li><strong>Encryption:</strong> refresh tokens are encrypted at rest and are
never logged or displayed.</li>
<li><strong>Third parties:</strong> short email previews are sent to
Anthropic's Claude API only for messages our rules can't confidently
classify, solely to determine a folder/urgency for that message.</li>
<li><strong>Deletion:</strong> disconnecting your account (see the dashboard)
revokes access and deletes your stored token immediately.</li>
</ul>
<p>Questions? Contact the person who invited you to this pilot.</p>
</body></html>"""


@app.get("/privacy")
def privacy():
    return PRIVACY_HTML


@app.get("/")
def index():
    if current_account() is not None:
        return redirect(url_for("dashboard"))
    return """<!doctype html><html><head><meta charset="utf-8">
<title>Inbox Triage</title></head><body style="font-family:sans-serif;max-width:480px;margin:6rem auto;text-align:center">
<h1>📬 Inbox Triage</h1>
<p style="color:#666">Automated priority sorting for your inbox.</p>
<p><a href="/login" style="display:inline-block;padding:0.7rem 1.5rem;background:#0969da;color:#fff;
border-radius:8px;text-decoration:none;font-weight:600">Sign in with Google</a></p>
<p style="font-size:0.8rem;color:#999"><a href="/privacy">Privacy policy</a></p>
</body></html>"""


@app.get("/login")
def login():
    flow = build_flow()
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    session["oauth_state"] = state
    return redirect(authorization_url)


@app.get("/oauth/callback")
def oauth_callback():
    expected_state = session.pop("oauth_state", None)
    if not expected_state or request.args.get("state") != expected_state:
        return "Login failed: invalid or expired state.", 400

    flow = build_flow()
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials

    if not creds.refresh_token:
        # Can happen if Google didn't grant offline access (e.g. the user
        # had already consented before without prompt=consent forcing a new
        # refresh token). Must not silently store an account with no way to
        # refresh access later.
        return (
            "Login didn't grant offline access — please try signing in again.",
            400,
        )

    from googleapiclient.discovery import build as build_service

    service = build_service("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()
    google_email = profile["emailAddress"]

    conn = get_db()
    account = upsert_account(conn, google_email=google_email, refresh_token=creds.refresh_token)

    session.clear()
    session["account_id"] = account.id
    session.permanent = True
    return redirect(url_for("dashboard"))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.post("/disconnect")
@login_required
def disconnect():
    disconnect_account(get_db(), g.account.id)
    session.clear()
    return redirect(url_for("index"))


@app.get("/dashboard")
@login_required
def dashboard():
    if g.account.status == "needs_reauth":
        return redirect(url_for("login"))

    config = load_config()
    html = render_dashboard_for_account(get_db(), g.account.id, config)
    return html


@app.get("/search")
@login_required
def search():
    query = (request.args.get("q") or "").strip()
    try:
        count = int(request.args.get("count", DEFAULT_RESULT_COUNT))
    except ValueError:
        count = DEFAULT_RESULT_COUNT

    if not query:
        return jsonify({"error": "Missing query"}), 400

    config = load_config()
    try:
        service = build_gmail_service_for_account(g.account)
    except RefreshError:
        return jsonify({"error": "Your Gmail connection has expired — please sign in again."}), 401

    try:
        results = run_search(query, service=service, config=config, count=count)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    return jsonify({"query": query, "results": results})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
