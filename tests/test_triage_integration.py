"""End-to-end smoke test using a fake Gmail service and a real local Postgres
— no real Gmail/Anthropic network calls. Verifies the full multi-tenant
pipeline (list active accounts -> per-account fetch -> rules -> LLM fallback
-> label application -> audit log) is wired correctly."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import triage
from src.config import Config, LabelColor, Rule
from src.db import get_connection


def _b64url(text: str) -> str:
    import base64

    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def make_fake_message(msg_id, thread_id, from_addr, subject, body_text, list_unsubscribe=None):
    headers = [
        {"name": "From", "value": from_addr},
        {"name": "Subject", "value": subject},
    ]
    if list_unsubscribe:
        headers.append({"name": "List-Unsubscribe", "value": list_unsubscribe})
    return {
        "id": msg_id,
        "threadId": thread_id,
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {
            "headers": headers,
            "mimeType": "text/plain",
            "body": {"data": _b64url(body_text)},
        },
    }


class FakeMessagesResource:
    def __init__(self, messages_by_id):
        self._messages = messages_by_id
        self.modify_calls = []

    def list(self, userId, q, pageToken=None, maxResults=100):
        return _Exec({"messages": [{"id": mid} for mid in self._messages], "nextPageToken": None})

    def get(self, userId, id, format):
        return _Exec(self._messages[id])

    def modify(self, userId, id, body):
        self.modify_calls.append((id, body))
        return _Exec({})


class FakeLabelsResource:
    def __init__(self):
        self.labels = {}  # name -> {"id": str, "color": dict | None}
        self._next_id = 1
        self.create_calls = []
        self.patch_calls = []

    def list(self, userId):
        return _Exec(
            {"labels": [{"id": v["id"], "name": k, "color": v["color"]} for k, v in self.labels.items()]}
        )

    def create(self, userId, body):
        name = body["name"]
        label_id = f"LABEL_{self._next_id}"
        self._next_id += 1
        color = body.get("color")
        self.labels[name] = {"id": label_id, "color": color}
        self.create_calls.append(name)
        return _Exec({"id": label_id, "name": name, "color": color})

    def patch(self, userId, id, body):
        self.patch_calls.append((id, body))
        for name, entry in self.labels.items():
            if entry["id"] == id:
                entry["color"] = body.get("color", entry["color"])
                return _Exec({"id": id, "name": name, "color": entry["color"]})
        raise KeyError(f"no fake label with id {id}")


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeUsersResource:
    def __init__(self, messages_by_id):
        self._messages = FakeMessagesResource(messages_by_id)
        self._labels = FakeLabelsResource()

    def messages(self):
        return self._messages

    def labels(self):
        return self._labels


class FakeGmailService:
    def __init__(self, messages_by_id):
        self._users = FakeUsersResource(messages_by_id)

    def users(self):
        return self._users


def make_config():
    return Config(
        folders=["Clients", "Internal", "Finance", "Vendors", "Newsletters", "Other"],
        default_folder="Other",
        urgency_tiers=["Urgent", "Today", "ThisWeek", "NoAction"],
        default_urgency="NoAction",
        gmail_query="in:inbox -label:Triage/Processed",
        label_prefix="Triage",
        remove_from_inbox=False,
        llm_model="claude-haiku-4-5-20251001",
        llm_max_body_chars=300,
        llm_enabled=True,
        rules=[
            Rule(
                name="Newsletter senders",
                folder="Newsletters",
                urgency="NoAction",
                conditions={"sender_local_part_prefix": ["no-reply", "noreply"]},
            ),
            Rule(
                name="Finance keywords",
                folder="Finance",
                urgency="ThisWeek",
                conditions={"subject_contains": ["invoice"]},
            ),
        ],
        folder_colors={"Newsletters": LabelColor(background="#999999", text="#ffffff")},
        urgency_colors={"NoAction": LabelColor(background="#cccccc", text="#000000")},
    )


class TriageIntegrationTestCase(unittest.TestCase):
    """Creates one or more real (throwaway) accounts in Postgres per test,
    each wired to its own fake Gmail service via a patched
    build_gmail_service_for_account, and cleans the accounts (and their
    cascaded classifications) up in tearDown."""

    def setUp(self):
        self.conn = get_connection()
        self._account_ids: list[int] = []
        self._services_by_account_id: dict[int, FakeGmailService] = {}

    def tearDown(self):
        if self._account_ids:
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM accounts WHERE id = ANY(%s)", (self._account_ids,))
            self.conn.commit()
        self.conn.close()

    def make_account(self, email: str, service: FakeGmailService) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO accounts (google_email, encrypted_refresh_token) VALUES (%s, %s) RETURNING id",
                (email, "placeholder"),
            )
            account_id = cur.fetchone()[0]
        self.conn.commit()
        self._account_ids.append(account_id)
        self._services_by_account_id[account_id] = service
        return account_id

    def _build_service_for_account(self, account):
        return self._services_by_account_id[account.id]

    def patches(self, config=None):
        return (
            patch("src.triage.load_config", return_value=config or make_config()),
            patch("src.triage.build_gmail_service_for_account", side_effect=self._build_service_for_account),
        )


class TestTriageIntegration(TriageIntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.messages = {
            "msg-rule-match": make_fake_message(
                "msg-rule-match", "thread1", "no-reply@github.com", "Your build passed", "Build succeeded."
            ),
            "msg-llm-fallback": make_fake_message(
                "msg-llm-fallback", "thread2", "someone@randomdomain.com", "Quick question", "Can we chat tomorrow?"
            ),
        }
        self.fake_service = FakeGmailService(self.messages)
        self.account_id = self.make_account("rule-and-llm@example.com", self.fake_service)

    def test_full_pipeline_rule_match_and_llm_fallback(self):
        from src.llm_classifier import LLMClassification

        fake_llm_result = LLMClassification(folder="Internal", urgency="ThisWeek", confidence=0.8)

        p1, p2 = self.patches()
        with p1, p2, patch("src.llm_classifier.classify_email", return_value=fake_llm_result):
            triage.run_all_accounts(dry_run=False, limit=None)

        calls_by_id = {mid: body for mid, body in self.fake_service.users().messages().modify_calls}
        self.assertIn("msg-rule-match", calls_by_id)
        self.assertIn("msg-llm-fallback", calls_by_id)

        label_names = set(self.fake_service.users().labels().labels.keys())
        self.assertIn("Triage/Newsletters", label_names)
        self.assertIn("Triage/Urgency/NoAction", label_names)
        self.assertIn("Triage/Internal", label_names)
        self.assertIn("Triage/Urgency/ThisWeek", label_names)
        self.assertIn("Triage/Processed", label_names)

        fake_labels = self.fake_service.users().labels().labels
        self.assertEqual(
            fake_labels["Triage/Newsletters"]["color"], {"backgroundColor": "#999999", "textColor": "#ffffff"}
        )
        self.assertEqual(
            fake_labels["Triage/Urgency/NoAction"]["color"], {"backgroundColor": "#cccccc", "textColor": "#000000"}
        )
        self.assertIsNone(fake_labels["Triage/Internal"]["color"])
        self.assertIsNone(fake_labels["Triage/Urgency/ThisWeek"]["color"])

        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT message_id, folder, urgency, method, snippet FROM classifications WHERE account_id = %s ORDER BY message_id",
                (self.account_id,),
            )
            rows_by_id = {r[0]: r for r in cur.fetchall()}

        self.assertEqual(rows_by_id["msg-rule-match"][1:4], ("Newsletters", "NoAction", "Newsletter senders"))
        self.assertEqual(rows_by_id["msg-llm-fallback"][1:4], ("Internal", "ThisWeek", "llm"))
        self.assertEqual(rows_by_id["msg-rule-match"][4], "Build succeeded.")
        self.assertEqual(rows_by_id["msg-llm-fallback"][4], "Can we chat tomorrow?")

    def test_dry_run_does_not_call_modify(self):
        from src.llm_classifier import LLMClassification

        fake_llm_result = LLMClassification(folder="Other", urgency="NoAction", confidence=0.5)

        p1, p2 = self.patches()
        with p1, p2, patch("src.llm_classifier.classify_email", return_value=fake_llm_result):
            triage.run_all_accounts(dry_run=True, limit=None)

        self.assertEqual(self.fake_service.users().messages().modify_calls, [])
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM classifications WHERE account_id = %s", (self.account_id,))
            count = cur.fetchone()[0]
        self.assertEqual(count, 2)

    def test_limit_restricts_number_processed(self):
        from src.llm_classifier import LLMClassification

        fake_llm_result = LLMClassification(folder="Other", urgency="NoAction", confidence=0.5)

        p1, p2 = self.patches()
        with p1, p2, patch("src.llm_classifier.classify_email", return_value=fake_llm_result):
            triage.run_all_accounts(dry_run=True, limit=1)

        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM classifications WHERE account_id = %s", (self.account_id,))
            count = cur.fetchone()[0]
        self.assertEqual(count, 1)


class TestSyncLabelColors(TriageIntegrationTestCase):
    def test_creates_and_colors_every_configured_label_upfront(self):
        # Empty inbox: no messages to classify, but every folder/urgency
        # label from config should still get created and colored.
        empty_service = FakeGmailService({})
        self.make_account("empty-inbox@example.com", empty_service)

        p1, p2 = self.patches()
        with p1, p2:
            triage.run_all_accounts(dry_run=False, limit=None)

        fake_labels = empty_service.users().labels().labels
        for folder in ["Clients", "Internal", "Finance", "Vendors", "Newsletters", "Other"]:
            self.assertIn(f"Triage/{folder}", fake_labels)
        for tier in ["Urgent", "Today", "ThisWeek", "NoAction"]:
            self.assertIn(f"Triage/Urgency/{tier}", fake_labels)

        self.assertEqual(
            fake_labels["Triage/Newsletters"]["color"], {"backgroundColor": "#999999", "textColor": "#ffffff"}
        )
        self.assertIsNone(fake_labels["Triage/Clients"]["color"])

    def test_skipped_in_dry_run(self):
        empty_service = FakeGmailService({})
        self.make_account("dry-run-empty@example.com", empty_service)

        p1, p2 = self.patches()
        with p1, p2:
            triage.run_all_accounts(dry_run=True, limit=None)

        self.assertEqual(empty_service.users().labels().labels, {})


class TestMultiAccountIsolation(TriageIntegrationTestCase):
    def test_one_accounts_failure_does_not_stop_the_others(self):
        good_messages = {
            "m1": make_fake_message("m1", "t1", "no-reply@github.com", "Build passed", "OK"),
        }
        good_service = FakeGmailService(good_messages)
        good_account_id = self.make_account("good-account@example.com", good_service)

        broken_account_id = self.make_account("broken-account@example.com", service=object())  # will raise when used as a Gmail service

        p1, p2 = self.patches()
        with p1, p2:
            triage.run_all_accounts(dry_run=False, limit=None)

        # The good account still got processed despite the other failing.
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM classifications WHERE account_id = %s", (good_account_id,))
            self.assertEqual(cur.fetchone()[0], 1)

        # The broken account's failure was recorded, not silently dropped or fatal.
        with self.conn.cursor() as cur:
            cur.execute("SELECT status, last_error FROM accounts WHERE id = %s", (broken_account_id,))
            status, last_error = cur.fetchone()
        self.assertEqual(status, "active")  # a pipeline error, not a dead token
        self.assertIsNotNone(last_error)

    def test_dead_refresh_token_marks_needs_reauth_and_continues(self):
        from google.auth.exceptions import RefreshError

        good_messages = {"m1": make_fake_message("m1", "t1", "no-reply@github.com", "Build passed", "OK")}
        good_service = FakeGmailService(good_messages)
        good_account_id = self.make_account("good-account-2@example.com", good_service)
        dead_account_id = self.make_account("dead-token@example.com", service=None)

        def build_service(account):
            if account.id == dead_account_id:
                raise RefreshError("invalid_grant: Token has been expired or revoked.")
            return self._services_by_account_id[account.id]

        with patch("src.triage.load_config", return_value=make_config()), patch(
            "src.triage.build_gmail_service_for_account", side_effect=build_service
        ):
            triage.run_all_accounts(dry_run=False, limit=None)

        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM classifications WHERE account_id = %s", (good_account_id,))
            self.assertEqual(cur.fetchone()[0], 1)

        with self.conn.cursor() as cur:
            cur.execute("SELECT status FROM accounts WHERE id = %s", (dead_account_id,))
            self.assertEqual(cur.fetchone()[0], "needs_reauth")


if __name__ == "__main__":
    unittest.main()
