from __future__ import annotations

from functools import wraps

from flask import g, redirect, session, url_for

from src.accounts import Account, get_account
from src.db import get_connection


def get_db():
    """One connection per request, stashed on flask.g and closed in
    teardown_appcontext (registered by init_app) — deliberately not pooled,
    see src/db.py::get_connection for why."""
    if "db" not in g:
        g.db = get_connection()
    return g.db


def close_db(exception=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def current_account() -> Account | None:
    """The logged-in account for this request, or None. Never trusts
    anything from the request other than the signed session cookie — this
    is the single tenant-isolation boundary every route relies on."""
    account_id = session.get("account_id")
    if account_id is None:
        return None
    return get_account(get_db(), account_id)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        account = current_account()
        if account is None or account.status == "disabled":
            return redirect(url_for("login"))
        g.account = account
        return view(*args, **kwargs)

    return wrapped
