import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config
from src.search import (
    Candidate,
    RankedResult,
    build_gmail_query,
    print_results,
    run_search,
)


def make_config():
    return Config(
        folders=["Clients", "Internal", "Finance", "Vendors", "Newsletters", "Other"],
        default_folder="Other",
        urgency_tiers=["Urgent", "Today", "ThisWeek", "NoAction"],
        default_urgency="NoAction",
        gmail_query="in:inbox",
        label_prefix="Triage",
        remove_from_inbox=False,
        llm_model="claude-haiku-4-5-20251001",
        llm_max_body_chars=300,
        llm_enabled=True,
        rules=[],
        folder_colors={},
        urgency_colors={},
    )


class TestBuildGmailQuery(unittest.TestCase):
    def test_empty_keywords_gives_empty_query(self):
        self.assertEqual(build_gmail_query([]), "")

    def test_single_word_keywords_joined_with_or(self):
        self.assertEqual(build_gmail_query(["business", "invoice"]), "business OR invoice")

    def test_multi_word_phrases_get_quoted(self):
        result = build_gmail_query(["quarterly report", "invoice"])
        self.assertEqual(result, '"quarterly report" OR invoice')

    def test_single_keyword_no_or(self):
        self.assertEqual(build_gmail_query(["business"]), "business")


class TestPrintResults(unittest.TestCase):
    def test_empty_results_prints_no_matches_message(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_results("business emails", [])
        self.assertIn("No relevant emails found", buf.getvalue())
        self.assertIn("business emails", buf.getvalue())

    def test_prints_subject_sender_reason_and_link(self):
        results = [
            {
                "message_id": "abc123",
                "subject": "Invoice due",
                "sender": '"Acme Corp" <billing@acme.com>',
                "received_at": "2026-01-01T00:00:00+00:00",
                "reason": "This is a business invoice.",
            }
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_results("business", results)
        output = buf.getvalue()
        self.assertIn("Invoice due", output)
        self.assertIn("Acme Corp", output)
        self.assertNotIn("billing@acme.com", output)  # cleaned sender, not raw header
        self.assertIn("This is a business invoice.", output)
        self.assertIn("#all/abc123", output)

    def test_numbers_results_in_order(self):
        results = [
            {"message_id": "a", "subject": "First", "sender": "a@b.com", "received_at": "", "reason": "r1"},
            {"message_id": "b", "subject": "Second", "sender": "a@b.com", "received_at": "", "reason": "r2"},
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_results("q", results)
        output = buf.getvalue()
        self.assertLess(output.index("1. First"), output.index("2. Second"))


class FakeMessage:
    def __init__(self, id, subject, sender, body_text, internal_date_ms="1700000000000"):
        self.id = id
        self.thread_id = f"t-{id}"
        self.subject = subject
        self.from_header = sender
        self.plain_text = body_text
        self.html = None
        self.list_unsubscribe = None
        self.internal_date_ms = internal_date_ms


class TestRunSearchIntegration(unittest.TestCase):
    def test_full_flow_narrows_candidates_to_ranked_results(self):
        fake_messages = {
            "m1": FakeMessage("m1", "Invoice from Acme", "billing@acme.com", "Please pay your invoice."),
            "m2": FakeMessage("m2", "Lunch plans?", "friend@example.com", "Want to grab lunch Friday?"),
        }

        with patch("src.search.derive_search_keywords", return_value=["invoice", "business"]), patch(
            "src.search.list_candidate_message_ids", return_value=list(fake_messages.keys())
        ), patch(
            "src.search.get_message", side_effect=lambda service, mid: fake_messages[mid]
        ), patch(
            "src.search.rank_candidates",
            return_value=[RankedResult(message_id="m1", reason="This is a business invoice.")],
        ):
            results = run_search("find my business emails", service=object(), config=make_config(), count=5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["message_id"], "m1")
        self.assertEqual(results[0]["subject"], "Invoice from Acme")
        self.assertEqual(results[0]["reason"], "This is a business invoice.")

    def test_no_keywords_derived_returns_empty_without_searching_gmail(self):
        with patch("src.search.derive_search_keywords", return_value=[]), patch(
            "src.search.list_candidate_message_ids"
        ) as mock_list:
            results = run_search("???", service=object(), config=make_config())

        self.assertEqual(results, [])
        mock_list.assert_not_called()

    def test_no_gmail_matches_returns_empty_without_ranking_call(self):
        with patch("src.search.derive_search_keywords", return_value=["business"]), patch(
            "src.search.list_candidate_message_ids", return_value=[]
        ), patch("src.search.rank_candidates") as mock_rank:
            results = run_search("business", service=object(), config=make_config())

        self.assertEqual(results, [])
        mock_rank.assert_not_called()

    def test_candidate_limit_truncates_gmail_matches_before_fetching(self):
        many_ids = [f"m{i}" for i in range(100)]
        fake_messages = {mid: FakeMessage(mid, "Subject", "a@b.com", "body") for mid in many_ids}
        fetched_ids = []

        def fake_get_message(service, mid):
            fetched_ids.append(mid)
            return fake_messages[mid]

        with patch("src.search.derive_search_keywords", return_value=["x"]), patch(
            "src.search.list_candidate_message_ids", return_value=many_ids
        ), patch("src.search.get_message", side_effect=fake_get_message), patch(
            "src.search.rank_candidates", return_value=[]
        ):
            run_search("x", service=object(), config=make_config(), candidate_limit=10)

        self.assertEqual(len(fetched_ids), 10)

    def test_ranked_message_id_not_in_candidates_is_silently_dropped(self):
        # Defensive: the LLM could in principle hallucinate an id not in the
        # candidate set. That shouldn't crash the search.
        fake_messages = {"m1": FakeMessage("m1", "Real", "a@b.com", "body")}

        with patch("src.search.derive_search_keywords", return_value=["x"]), patch(
            "src.search.list_candidate_message_ids", return_value=["m1"]
        ), patch(
            "src.search.get_message", side_effect=lambda service, mid: fake_messages[mid]
        ), patch(
            "src.search.rank_candidates",
            return_value=[RankedResult(message_id="does-not-exist", reason="hallucinated")],
        ):
            results = run_search("x", service=object(), config=make_config())

        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
