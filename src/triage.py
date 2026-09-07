from __future__ import annotations

import argparse
import sys

from google.auth.exceptions import RefreshError

from src.accounts import Account, list_active_accounts, mark_error, mark_needs_reauth, mark_synced
from src.body_extract import extract_snippet
from src.config import load_config
from src.credentials_store import build_gmail_service_for_account
from src.db import get_connection, log_classification
from src.gmail_client import LabelManager, apply_labels, get_message, list_candidate_message_ids
from src.rules import EmailFeatures, classify_with_rules


def sync_label_colors(label_manager: LabelManager, config) -> None:
    """Create/recolor every configured folder + urgency label up front, so
    color changes in config.yaml take effect even on a run with no matching
    messages (label colors otherwise only update for labels a message needs)."""
    for folder in config.folders:
        label_manager.get_or_create(f"{config.label_prefix}/{folder}", color=config.folder_colors.get(folder))
    for tier in config.urgency_tiers:
        label_manager.get_or_create(f"{config.label_prefix}/Urgency/{tier}", color=config.urgency_colors.get(tier))


def run_for_account(account: Account, config, conn, service, *, dry_run: bool, limit: int | None) -> None:
    """Classify and label one account's candidate messages. Raises on any
    Gmail/DB error — the caller (run_all_accounts) decides how to isolate
    that failure from other accounts; this function doesn't catch anything
    itself so a single account's own manual/CLI use still sees real errors."""
    label_manager = LabelManager(service)
    if not dry_run:
        sync_label_colors(label_manager, config)

    message_ids = list_candidate_message_ids(service, config.gmail_query)
    if limit:
        message_ids = message_ids[:limit]

    print(f"[{account.google_email}] Found {len(message_ids)} candidate message(s) matching: {config.gmail_query!r}")

    processed_label_id = None if dry_run else label_manager.get_or_create(f"{config.label_prefix}/Processed")

    for message_id in message_ids:
        msg = get_message(service, message_id)
        features = EmailFeatures.from_headers(msg.from_header, msg.subject, msg.list_unsubscribe)

        # Extracted for every message (not just ones needing the LLM) so the
        # dashboard can show a basic preview regardless of how it was classified.
        snippet = extract_snippet(plain_text=msg.plain_text, html=msg.html, max_chars=config.llm_max_body_chars)

        rule_match = classify_with_rules(features, config)
        if rule_match:
            folder, urgency, method, confidence = rule_match.folder, rule_match.urgency, rule_match.rule_name, 1.0
        elif config.llm_enabled:
            from src.llm_classifier import classify_email  # deferred: avoid requiring anthropic if unused

            try:
                result = classify_email(sender=msg.from_header, subject=msg.subject, snippet=snippet, config=config)
                folder, urgency, method, confidence = result.folder, result.urgency, "llm", result.confidence
            except Exception as exc:
                print(f"  LLM classification failed ({exc}); using default folder/urgency.", file=sys.stderr)
                folder, urgency, method, confidence = config.default_folder, config.default_urgency, "llm_error", 0.0
        else:
            folder, urgency, method, confidence = config.default_folder, config.default_urgency, "default", 0.0

        will_archive = config.remove_from_inbox

        print(f"  [{method}] {msg.subject!r} from {msg.from_header!r} -> {folder} / {urgency}")

        if not dry_run:
            folder_label_id = label_manager.get_or_create(
                f"{config.label_prefix}/{folder}", color=config.folder_colors.get(folder)
            )
            urgency_label_id = label_manager.get_or_create(
                f"{config.label_prefix}/Urgency/{urgency}", color=config.urgency_colors.get(urgency)
            )

            add_ids = [folder_label_id, urgency_label_id, processed_label_id]
            remove_ids = ["INBOX"] if will_archive else []
            apply_labels(service, message_id, add_ids, remove_ids)

        log_classification(
            conn,
            account_id=account.id,
            message_id=msg.id,
            thread_id=msg.thread_id,
            sender=msg.from_header,
            subject=msg.subject,
            folder=folder,
            urgency=urgency,
            method=method,
            confidence=confidence,
            archived=will_archive and not dry_run,
            snippet=snippet,
        )


def run_all_accounts(*, dry_run: bool = False, limit: int | None = None) -> None:
    """Loop every active account, classifying and labeling its mail. One
    account's failure never stops the others: a dead/expired refresh token
    marks that account needs_reauth and moves on; any other error is logged
    to the account's last_error and retried next cycle."""
    config = load_config()
    conn = get_connection()

    for account in list_active_accounts(conn):
        try:
            service = build_gmail_service_for_account(account)
        except RefreshError as exc:
            print(f"[{account.google_email}] Refresh token invalid, marking needs_reauth: {exc}", file=sys.stderr)
            mark_needs_reauth(conn, account.id, str(exc))
            conn.rollback()
            continue

        try:
            run_for_account(account, config, conn, service, dry_run=dry_run, limit=limit)
            if not dry_run:
                mark_synced(conn, account.id)
        except Exception as exc:
            print(f"[{account.google_email}] Triage run failed: {exc}", file=sys.stderr)
            mark_error(conn, account.id, str(exc))
            conn.rollback()
            continue

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Triage unread/recent Gmail for every active account.")
    parser.add_argument("--dry-run", action="store_true", help="Classify and log, but don't touch Gmail labels.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N candidate messages per account.")
    args = parser.parse_args()

    try:
        run_all_accounts(dry_run=args.dry_run, limit=args.limit)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
