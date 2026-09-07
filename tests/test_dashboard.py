import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta, timezone

from src.accounts import get_summary_cache
from src.config import Config, LabelColor
from src.dashboard import (
    TodoItem,
    _display_sender,
    _group_by_urgency,
    _plain_fallback_summary,
    _tier_icon,
    _time_ago,
    build_summary_prompt,
    fetch_recent_items,
    group_by_folder,
    render_dashboard_for_account,
    render_dashboard_html,
    split_actionable,
)
from src.db import get_connection, log_classification


def make_config():
    return Config(
        folders=["Clients", "Newsletters", "Other"],
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
        urgency_colors={
            "Urgent": LabelColor(background="#cc3a21", text="#ffffff"),
            "Today": LabelColor(background="#ffad47", text="#000000"),
            "ThisWeek": LabelColor(background="#fad165", text="#000000"),
            "NoAction": LabelColor(background="#cccccc", text="#000000"),
        },
    )


def make_item(
    urgency,
    subject="Subject",
    sender="a@b.com",
    folder="Other",
    message_id="m1",
    created_at="2026-09-04T12:00:00+00:00",
    snippet="",
):
    return TodoItem(
        message_id=message_id,
        sender=sender,
        subject=subject,
        folder=folder,
        urgency=urgency,
        created_at=created_at,
        snippet=snippet,
    )


class TestSplitActionable(unittest.TestCase):
    def test_separates_actionable_from_noaction(self):
        items = [make_item("Urgent"), make_item("NoAction"), make_item("Today")]
        actionable, quiet = split_actionable(items, make_config())
        self.assertEqual(len(actionable), 2)
        self.assertEqual(quiet, 1)

    def test_sorts_by_urgency_most_urgent_first(self):
        items = [make_item("ThisWeek"), make_item("Urgent"), make_item("Today")]
        actionable, _ = split_actionable(items, make_config())
        self.assertEqual([i.urgency for i in actionable], ["Urgent", "Today", "ThisWeek"])

    def test_empty_input(self):
        actionable, quiet = split_actionable([], make_config())
        self.assertEqual(actionable, [])
        self.assertEqual(quiet, 0)

    def test_all_noaction_gives_empty_actionable(self):
        items = [make_item("NoAction"), make_item("NoAction")]
        actionable, quiet = split_actionable(items, make_config())
        self.assertEqual(actionable, [])
        self.assertEqual(quiet, 2)


class TestBuildSummaryPrompt(unittest.TestCase):
    def test_includes_each_item(self):
        items = [make_item("Urgent", subject="Server down"), make_item("Today", subject="Invoice due")]
        prompt = build_summary_prompt(items)
        self.assertIn("Server down", prompt)
        self.assertIn("Invoice due", prompt)
        self.assertIn("Urgent", prompt)

    def test_empty_list_still_produces_valid_prompt(self):
        prompt = build_summary_prompt([])
        self.assertIsInstance(prompt, str)
        self.assertGreater(len(prompt), 0)

    def test_includes_body_snippet_for_better_context(self):
        items = [make_item("Urgent", subject="Server down", snippet="Prod database is unreachable since 3pm.")]
        prompt = build_summary_prompt(items)
        self.assertIn("Prod database is unreachable since 3pm.", prompt)

    def test_truncates_snippet_to_keep_prompt_small(self):
        long_snippet = "x" * 500
        items = [make_item("Urgent", snippet=long_snippet)]
        prompt = build_summary_prompt(items)
        self.assertNotIn("x" * 200, prompt)  # the full 500-char snippet must not appear
        self.assertIn("x" * 100, prompt)  # but a 100-char slice of it should

    def test_caps_detailed_items_and_summarizes_the_rest(self):
        items = [make_item("Today", subject=f"Item {i}", message_id=f"m{i}") for i in range(20)]
        prompt = build_summary_prompt(items)
        self.assertIn("Item 0", prompt)
        self.assertIn("Item 11", prompt)
        self.assertNotIn("Item 12", prompt)
        self.assertIn("+8 more", prompt)

    def test_no_overflow_note_when_under_the_cap(self):
        items = [make_item("Today", subject=f"Item {i}", message_id=f"m{i}") for i in range(5)]
        prompt = build_summary_prompt(items)
        self.assertNotIn("more lower-priority", prompt)


class TestPlainFallbackSummary(unittest.TestCase):
    def test_no_actionable_items(self):
        self.assertIn("Nothing needs your attention", _plain_fallback_summary([]))

    def test_lists_items_when_present(self):
        summary = _plain_fallback_summary([make_item("Urgent", subject="Fix prod")])
        self.assertIn("Fix prod", summary)
        self.assertIn("Urgent", summary)

    def test_caps_at_ten_items(self):
        items = [make_item("Today", subject=f"Item {i}") for i in range(15)]
        summary = _plain_fallback_summary(items)
        self.assertEqual(summary.count("Item"), 10)


class TestDisplaySender(unittest.TestCase):
    def test_quoted_display_name_with_long_address(self):
        raw = '"Anthropic Ireland, Limited" <invoice+statements+acct_1REyrSBNUnCSzfs9@stripe.com>'
        self.assertEqual(_display_sender(raw), "Anthropic Ireland, Limited")

    def test_unquoted_display_name(self):
        self.assertEqual(_display_sender("Google <no-reply@accounts.google.com>"), "Google")

    def test_bare_address_with_no_display_name(self):
        self.assertEqual(_display_sender("no-reply@github.com"), "no-reply@github.com")

    def test_empty_display_name_falls_back_to_address(self):
        self.assertEqual(_display_sender("<jane@example.com>"), "jane@example.com")

    def test_never_shows_both_name_and_bracketed_address(self):
        raw = '"Long Display Name Inc." <some.very.long.address+tag@example.com>'
        result = _display_sender(raw)
        self.assertNotIn("<", result)
        self.assertNotIn("@example.com", result)


class TestTimeAgo(unittest.TestCase):
    def test_just_now_for_recent_timestamp(self):
        ts = datetime.now(timezone.utc).isoformat()
        self.assertEqual(_time_ago(ts), "just now")

    def test_minutes_ago(self):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self.assertEqual(_time_ago(ts), "5m ago")

    def test_hours_ago(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        self.assertEqual(_time_ago(ts), "3h ago")

    def test_days_ago(self):
        ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.assertEqual(_time_ago(ts), "2d ago")

    def test_handles_malformed_timestamp_gracefully(self):
        self.assertEqual(_time_ago("not-a-timestamp"), "")

    def test_handles_naive_timestamp_without_crashing(self):
        # created_at is always written with a UTC offset by log_classification,
        # but defend against a naive timestamp rather than raising.
        result = _time_ago("2020-01-01T00:00:00")
        self.assertTrue(result == "just now" or result.endswith("ago"))


class TestTierIcon(unittest.TestCase):
    def test_known_ranks_get_distinct_icons(self):
        icons = {_tier_icon(r) for r in range(4)}
        self.assertEqual(len(icons), 4)

    def test_out_of_range_rank_falls_back(self):
        self.assertEqual(_tier_icon(99), "⚪")


class TestGroupByUrgency(unittest.TestCase):
    def test_groups_consecutive_same_urgency_items(self):
        items = [make_item("Urgent"), make_item("Urgent"), make_item("Today")]
        sections = _group_by_urgency(items, make_config())
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0][0], "Urgent")
        self.assertEqual(len(sections[0][2]), 2)
        self.assertEqual(sections[1][0], "Today")
        self.assertEqual(len(sections[1][2]), 1)

    def test_empty_input_gives_no_sections(self):
        self.assertEqual(_group_by_urgency([], make_config()), [])

    def test_includes_correct_rank_per_section(self):
        items = [make_item("Today")]
        sections = _group_by_urgency(items, make_config())
        self.assertEqual(sections[0][1], make_config().urgency_rank("Today"))


class TestGroupByFolder(unittest.TestCase):
    def test_every_configured_folder_appears_even_if_empty(self):
        # make_config()'s folders are Clients, Newsletters, Other.
        groups = group_by_folder([], make_config())
        self.assertEqual([f for f, _ in groups], ["Clients", "Newsletters", "Other"])
        self.assertTrue(all(items == [] for _, items in groups))

    def test_items_land_in_their_folder(self):
        items = [
            make_item("Urgent", folder="Clients", message_id="c1"),
            make_item("NoAction", folder="Newsletters", message_id="n1"),
            make_item("NoAction", folder="Newsletters", message_id="n2"),
        ]
        groups = dict(group_by_folder(items, make_config()))
        self.assertEqual(len(groups["Clients"]), 1)
        self.assertEqual(len(groups["Newsletters"]), 2)
        self.assertEqual(groups["Other"], [])

    def test_preserves_config_folder_order_regardless_of_item_order(self):
        items = [make_item("NoAction", folder="Other"), make_item("Urgent", folder="Clients")]
        groups = group_by_folder(items, make_config())
        self.assertEqual([f for f, _ in groups], ["Clients", "Newsletters", "Other"])

    def test_folder_not_in_config_still_included_as_leftover(self):
        # Defensive case: a rule referencing a folder name with a typo, not in
        # config.folders, shouldn't silently disappear from the dashboard.
        items = [make_item("NoAction", folder="TypoFolder")]
        groups = dict(group_by_folder(items, make_config()))
        self.assertIn("TypoFolder", groups)
        self.assertEqual(len(groups["TypoFolder"]), 1)


class TestRenderDashboardHtml(unittest.TestCase):
    def test_escapes_html_in_subject_and_sender(self):
        items = [make_item("Urgent", subject="<script>alert(1)</script>", sender="<b>evil</b>@x.com")]
        html = render_dashboard_html(
            summary_text="ok", actionable=items, quiet_count=0, generated_at="now", config=make_config()
        )
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<b>evil</b>@x.com", html)

    def test_shows_clean_display_name_not_raw_from_header(self):
        items = [
            make_item(
                "ThisWeek",
                sender='"Anthropic Ireland, Limited" <invoice+statements+acct_1REyrSBNUnCSzfs9@stripe.com>',
            )
        ]
        html = render_dashboard_html(
            summary_text="ok", actionable=items, quiet_count=0, generated_at="now", config=make_config()
        )
        self.assertIn("Anthropic Ireland, Limited", html)
        self.assertNotIn("invoice+statements", html)

    def test_escapes_html_in_summary_text(self):
        html = render_dashboard_html(
            summary_text="<script>bad()</script>", actionable=[], quiet_count=0, generated_at="now", config=make_config()
        )
        self.assertNotIn("<script>bad()</script>", html)

    def test_includes_gmail_deep_link(self):
        items = [make_item("Urgent", message_id="abc123")]
        html = render_dashboard_html(
            summary_text="ok", actionable=items, quiet_count=0, generated_at="now", config=make_config()
        )
        self.assertIn("#all/abc123", html)

    def test_shows_empty_state_when_no_actionable_items(self):
        html = render_dashboard_html(
            summary_text="ok", actionable=[], quiet_count=5, generated_at="now", config=make_config()
        )
        self.assertIn("all caught up", html)
        self.assertIn("5 newsletter", html)

    def test_has_auto_refresh_meta_tag(self):
        html = render_dashboard_html(
            summary_text="ok", actionable=[], quiet_count=0, generated_at="now", config=make_config()
        )
        self.assertIn('http-equiv="refresh"', html)
        self.assertIn('content="300"', html)

    def test_shows_snippet_preview_under_item(self):
        items = [make_item("Urgent", snippet="Can you review the attached contract by Friday?")]
        html = render_dashboard_html(
            summary_text="ok", actionable=items, quiet_count=0, generated_at="now", config=make_config()
        )
        self.assertIn("Can you review the attached contract by Friday?", html)

    def test_omits_snippet_block_when_no_snippet(self):
        items = [make_item("Urgent", snippet="")]
        html = render_dashboard_html(
            summary_text="ok", actionable=items, quiet_count=0, generated_at="now", config=make_config()
        )
        self.assertNotIn('class="snippet"', html)

    def test_truncates_long_snippet_to_200_chars(self):
        long_snippet = "word " * 100
        items = [make_item("Urgent", snippet=long_snippet)]
        html = render_dashboard_html(
            summary_text="ok", actionable=items, quiet_count=0, generated_at="now", config=make_config()
        )
        # Extract just the snippet div's content to check its length, not the whole page.
        start = html.index('class="snippet"')
        rendered_snippet = html[start : start + 260]
        self.assertLessEqual(len(rendered_snippet.split(">", 1)[1].split("<")[0]), 200)

    def test_escapes_html_in_snippet(self):
        items = [make_item("Urgent", snippet="<img src=x onerror=alert(1)>")]
        html = render_dashboard_html(
            summary_text="ok", actionable=items, quiet_count=0, generated_at="now", config=make_config()
        )
        self.assertNotIn("<img src=x onerror=alert(1)>", html)
        self.assertIn("&lt;img", html)

    def test_uses_configured_urgency_color(self):
        items = [make_item("Urgent")]
        html = render_dashboard_html(
            summary_text="ok", actionable=items, quiet_count=0, generated_at="now", config=make_config()
        )
        self.assertIn("#cc3a21", html)

    def test_renders_a_section_header_per_urgency_tier_present(self):
        items = [make_item("Urgent", subject="A"), make_item("Today", subject="B")]
        html = render_dashboard_html(
            summary_text="ok", actionable=items, quiet_count=0, generated_at="now", config=make_config()
        )
        self.assertIn('<div class="section-title">🔴 Urgent</div>', html)
        self.assertIn('<div class="section-title">🟠 Today</div>', html)

    def test_renders_stat_pill_with_count_per_tier(self):
        items = [make_item("Urgent"), make_item("Urgent"), make_item("Today")]
        html = render_dashboard_html(
            summary_text="ok", actionable=items, quiet_count=0, generated_at="now", config=make_config()
        )
        self.assertIn("2 Urgent", html)
        self.assertIn("1 Today", html)

    def test_no_stat_pills_when_no_actionable_items(self):
        html = render_dashboard_html(
            summary_text="ok", actionable=[], quiet_count=0, generated_at="now", config=make_config()
        )
        self.assertIn('<div class="stats"></div>', html)

    def test_respects_prefers_color_scheme_dark(self):
        html = render_dashboard_html(
            summary_text="ok", actionable=[], quiet_count=0, generated_at="now", config=make_config()
        )
        self.assertIn("prefers-color-scheme: dark", html)

    def test_shows_every_configured_folder_as_a_category(self):
        html = render_dashboard_html(
            summary_text="ok", actionable=[], quiet_count=0, generated_at="now", config=make_config(), all_items=[]
        )
        for folder in ["Clients", "Newsletters", "Other"]:
            self.assertIn(f">{folder}<", html)

    def test_empty_folder_shows_no_messages_placeholder(self):
        html = render_dashboard_html(
            summary_text="ok", actionable=[], quiet_count=0, generated_at="now", config=make_config(), all_items=[]
        )
        self.assertIn("No messages", html)

    def test_folder_with_items_shows_them_in_its_category(self):
        items = [make_item("NoAction", folder="Newsletters", subject="Weekly digest")]
        html = render_dashboard_html(
            summary_text="ok",
            actionable=[],
            quiet_count=1,
            generated_at="now",
            config=make_config(),
            all_items=items,
        )
        self.assertIn("Weekly digest", html)

    def test_category_count_reflects_number_of_items(self):
        items = [make_item("NoAction", folder="Other", message_id=f"m{i}") for i in range(3)]
        html = render_dashboard_html(
            summary_text="ok",
            actionable=[],
            quiet_count=3,
            generated_at="now",
            config=make_config(),
            all_items=items,
        )
        self.assertIn('Other<span class="cat-count">3</span>', html)

    def test_caps_items_shown_per_category_and_notes_the_rest(self):
        items = [make_item("NoAction", folder="Newsletters", message_id=f"m{i}") for i in range(30)]
        html = render_dashboard_html(
            summary_text="ok",
            actionable=[],
            quiet_count=30,
            generated_at="now",
            config=make_config(),
            all_items=items,
        )
        self.assertIn("+5 more not shown", html)

    def test_falls_back_to_actionable_when_all_items_not_given(self):
        # Backward-compatible default for callers that only care about the
        # todo list, not the category browser.
        items = [make_item("Urgent", folder="Clients")]
        html = render_dashboard_html(
            summary_text="ok", actionable=items, quiet_count=0, generated_at="now", config=make_config()
        )
        self.assertIn('Clients<span class="cat-count">1</span>', html)


class DashboardPostgresTestCase(unittest.TestCase):
    """Shared setup for dashboard tests that hit real Postgres: creates a
    throwaway account per test and cleans it (and its cascaded
    classifications) up in tearDown."""

    def setUp(self):
        self.conn = get_connection()
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO accounts (google_email, encrypted_refresh_token) VALUES (%s, %s) RETURNING id",
                (f"dashboard-test-{id(self)}@example.com", "placeholder"),
            )
            self.account_id = cur.fetchone()[0]
        self.conn.commit()

    def tearDown(self):
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM accounts WHERE id = %s", (self.account_id,))
        self.conn.commit()
        self.conn.close()

    def log(self, **kwargs):
        defaults = dict(
            account_id=self.account_id,
            thread_id="t1",
            sender="a@b.com",
            method="rule",
            confidence=1.0,
            archived=False,
        )
        defaults.update(kwargs)
        log_classification(self.conn, **defaults)


class TestFetchRecentItems(DashboardPostgresTestCase):
    def test_fetches_logged_classification(self):
        self.log(message_id="m1", subject="Test", folder="Finance", urgency="Today", snippet="Please pay by end of month.")
        items = fetch_recent_items(self.conn, self.account_id, since_hours=24)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].subject, "Test")
        self.assertEqual(items[0].snippet, "Please pay by end of month.")

    def test_dedupes_same_message_logged_multiple_times_keeping_latest(self):
        self.log(message_id="m1", subject="First pass", folder="Other", urgency="NoAction", method="rule")
        self.log(message_id="m1", subject="Second pass, corrected", folder="Finance", urgency="Urgent", method="llm", confidence=0.9)
        items = fetch_recent_items(self.conn, self.account_id, since_hours=24)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].subject, "Second pass, corrected")
        self.assertEqual(items[0].urgency, "Urgent")

    def test_excludes_items_outside_lookback_window(self):
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO classifications
                   (account_id, message_id, thread_id, sender, subject, folder, urgency, method, confidence, archived, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (self.account_id, "old1", "t1", "a@b.com", "Old", "Other", "NoAction", "rule", 1.0, False, "2020-01-01T00:00:00+00:00"),
            )
        self.conn.commit()
        items = fetch_recent_items(self.conn, self.account_id, since_hours=24)
        self.assertEqual(items, [])

    def test_only_returns_items_for_the_given_account(self):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO accounts (google_email, encrypted_refresh_token) VALUES (%s, %s) RETURNING id",
                ("other-account-isolation@example.com", "placeholder"),
            )
            other_account_id = cur.fetchone()[0]
        self.conn.commit()
        try:
            self.log(message_id="mine", subject="Mine", folder="Other", urgency="NoAction")
            log_classification(
                self.conn,
                account_id=other_account_id,
                message_id="theirs",
                thread_id="t",
                sender="a@b.com",
                subject="Theirs",
                folder="Other",
                urgency="NoAction",
                method="rule",
                confidence=1.0,
                archived=False,
            )
            items = fetch_recent_items(self.conn, self.account_id, since_hours=24)
            self.assertEqual([i.subject for i in items], ["Mine"])
        finally:
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM accounts WHERE id = %s", (other_account_id,))
            self.conn.commit()


class TestRenderDashboardForAccount(DashboardPostgresTestCase):
    def test_falls_back_gracefully_when_llm_call_fails(self):
        self.log(message_id="m1", subject="Urgent thing", folder="Finance", urgency="Urgent")
        with patch("src.dashboard.generate_llm_summary", side_effect=RuntimeError("no credits")):
            html = render_dashboard_for_account(self.conn, self.account_id, make_config())

        self.assertIn("AI summary unavailable", html)
        self.assertIn("Urgent thing", html)

    def test_skips_llm_call_entirely_when_nothing_actionable(self):
        self.log(message_id="m1", subject="Newsletter", folder="Newsletters", urgency="NoAction")
        with patch("src.dashboard.generate_llm_summary") as mock_llm:
            html = render_dashboard_for_account(self.conn, self.account_id, make_config())
        mock_llm.assert_not_called()
        self.assertIn("Nothing needs your attention", html)

    def _log_urgent(self, message_id, subject="Urgent thing"):
        self.log(message_id=message_id, subject=subject, folder="Finance", urgency="Urgent")

    def test_reuses_cached_summary_when_actionable_set_is_unchanged(self):
        self._log_urgent("m1")
        with patch("src.dashboard.generate_llm_summary", return_value="First summary") as mock_llm:
            render_dashboard_for_account(self.conn, self.account_id, make_config())
        mock_llm.assert_called_once()

        # Second render: same actionable message, nothing new — must not
        # call the LLM again, and the cached summary text should be reused.
        with patch("src.dashboard.generate_llm_summary", return_value="Should not appear") as mock_llm_2:
            html = render_dashboard_for_account(self.conn, self.account_id, make_config())
        mock_llm_2.assert_not_called()
        self.assertIn("First summary", html)

    def test_calls_llm_again_when_a_new_actionable_message_appears(self):
        self._log_urgent("m1")
        with patch("src.dashboard.generate_llm_summary", return_value="First summary"):
            render_dashboard_for_account(self.conn, self.account_id, make_config())

        self._log_urgent("m2", subject="A second urgent thing")
        with patch("src.dashboard.generate_llm_summary", return_value="Second summary") as mock_llm:
            html = render_dashboard_for_account(self.conn, self.account_id, make_config())
        mock_llm.assert_called_once()
        self.assertIn("Second summary", html)

    def test_cache_is_saved_to_the_account_after_a_real_summary_call(self):
        self._log_urgent("m1")
        with patch("src.dashboard.generate_llm_summary", return_value="Cached text"):
            render_dashboard_for_account(self.conn, self.account_id, make_config())
        cached = get_summary_cache(self.conn, self.account_id)
        self.assertEqual(cached, (["m1"], "Cached text"))

    def test_two_accounts_have_independent_summary_caches(self):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO accounts (google_email, encrypted_refresh_token) VALUES (%s, %s) RETURNING id",
                ("other-cache-isolation@example.com", "placeholder"),
            )
            other_account_id = cur.fetchone()[0]
        self.conn.commit()
        try:
            self._log_urgent("m1")
            log_classification(
                self.conn,
                account_id=other_account_id,
                message_id="m1",
                thread_id="t",
                sender="a@b.com",
                subject="Different account, same message id",
                folder="Finance",
                urgency="Urgent",
                method="rule",
                confidence=1.0,
                archived=False,
            )
            with patch("src.dashboard.generate_llm_summary", return_value="Mine"):
                render_dashboard_for_account(self.conn, self.account_id, make_config())
            with patch("src.dashboard.generate_llm_summary", return_value="Theirs"):
                render_dashboard_for_account(self.conn, other_account_id, make_config())

            self.assertEqual(get_summary_cache(self.conn, self.account_id), (["m1"], "Mine"))
            self.assertEqual(get_summary_cache(self.conn, other_account_id), (["m1"], "Theirs"))
        finally:
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM accounts WHERE id = %s", (other_account_id,))
            self.conn.commit()


if __name__ == "__main__":
    unittest.main()
