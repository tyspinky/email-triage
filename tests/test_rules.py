import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config, Rule
from src.rules import EmailFeatures, classify_with_rules


def make_config(rules: list[Rule], default_folder="Other", default_urgency="NoAction") -> Config:
    return Config(
        folders=["Clients", "Internal", "Finance", "Vendors", "Newsletters", "Other"],
        default_folder=default_folder,
        urgency_tiers=["Urgent", "Today", "ThisWeek", "NoAction"],
        default_urgency=default_urgency,
        gmail_query="in:inbox",
        label_prefix="Triage",
        remove_from_inbox=False,
        llm_model="claude-haiku-4-5-20251001",
        llm_max_body_chars=300,
        llm_enabled=True,
        rules=rules,
        folder_colors={},
        urgency_colors={},
    )


NEWSLETTER_RULE = Rule(
    name="Newsletter senders",
    folder="Newsletters",
    urgency="NoAction",
    conditions={"sender_local_part_prefix": ["no-reply", "noreply", "newsletter"]},
)

UNSUBSCRIBE_RULE = Rule(
    name="Has unsubscribe header",
    folder="Newsletters",
    urgency="NoAction",
    conditions={"has_list_unsubscribe": True},
)

CLIENT_DOMAIN_RULE = Rule(
    name="Known client domain",
    folder="Clients",
    urgency="Today",
    conditions={"sender_domain": ["acmeclient.com"]},
)

FINANCE_SUBJECT_RULE = Rule(
    name="Finance keywords",
    folder="Finance",
    urgency="ThisWeek",
    conditions={"subject_contains": ["invoice", "payment"]},
)

VENDOR_EXACT_ADDRESS_RULE = Rule(
    name="Exact vendor address",
    folder="Vendors",
    urgency="ThisWeek",
    conditions={"sender_address": ["billing@stripe.com"]},
)

MULTI_CONDITION_RULE = Rule(
    name="Client domain AND urgent subject",
    folder="Clients",
    urgency="Urgent",
    conditions={"sender_domain": ["acmeclient.com"], "subject_contains": ["urgent", "asap"]},
)


class TestEmailFeatures(unittest.TestCase):
    def test_extracts_address_from_display_name_format(self):
        f = EmailFeatures.from_headers("Jane Doe <jane@example.com>", "Hi", None)
        self.assertEqual(f.sender_address, "jane@example.com")
        self.assertEqual(f.sender_local_part, "jane")
        self.assertEqual(f.sender_domain, "example.com")

    def test_extracts_bare_address(self):
        f = EmailFeatures.from_headers("jane@example.com", "Hi", None)
        self.assertEqual(f.sender_address, "jane@example.com")

    def test_lowercases_address(self):
        f = EmailFeatures.from_headers("Jane <JANE@EXAMPLE.COM>", "Hi", None)
        self.assertEqual(f.sender_address, "jane@example.com")
        self.assertEqual(f.sender_domain, "example.com")

    def test_has_list_unsubscribe_true_when_header_present(self):
        f = EmailFeatures.from_headers("a@b.com", "Hi", "<mailto:unsub@b.com>")
        self.assertTrue(f.has_list_unsubscribe)

    def test_has_list_unsubscribe_false_when_absent(self):
        f = EmailFeatures.from_headers("a@b.com", "Hi", None)
        self.assertFalse(f.has_list_unsubscribe)


class TestRulesEngine(unittest.TestCase):
    def test_no_rules_match_returns_none(self):
        config = make_config([CLIENT_DOMAIN_RULE])
        features = EmailFeatures.from_headers("random@unknown.com", "Hello", None)
        self.assertIsNone(classify_with_rules(features, config))

    def test_sender_local_part_prefix_matches_noreply(self):
        config = make_config([NEWSLETTER_RULE])
        features = EmailFeatures.from_headers("no-reply@github.com", "Your build passed", None)
        match = classify_with_rules(features, config)
        self.assertIsNotNone(match)
        self.assertEqual(match.folder, "Newsletters")
        self.assertEqual(match.urgency, "NoAction")
        self.assertEqual(match.rule_name, "Newsletter senders")

    def test_sender_local_part_prefix_is_case_insensitive(self):
        config = make_config([NEWSLETTER_RULE])
        features = EmailFeatures.from_headers("No-Reply@Github.com", "Hi", None)
        self.assertIsNotNone(classify_with_rules(features, config))

    def test_sender_local_part_prefix_does_not_match_substring_mid_string(self):
        # "bignoreply" does not *start with* "noreply" — should not match.
        config = make_config([NEWSLETTER_RULE])
        features = EmailFeatures.from_headers("bignoreply@example.com", "Hi", None)
        self.assertIsNone(classify_with_rules(features, config))

    def test_has_list_unsubscribe_matches(self):
        config = make_config([UNSUBSCRIBE_RULE])
        features = EmailFeatures.from_headers("updates@somesaas.com", "Weekly digest", "<mailto:x@y.com>")
        match = classify_with_rules(features, config)
        self.assertEqual(match.folder, "Newsletters")

    def test_sender_domain_exact_match(self):
        config = make_config([CLIENT_DOMAIN_RULE])
        features = EmailFeatures.from_headers("someone@acmeclient.com", "Project update", None)
        match = classify_with_rules(features, config)
        self.assertEqual(match.folder, "Clients")
        self.assertEqual(match.urgency, "Today")

    def test_sender_domain_does_not_match_subdomain_by_default(self):
        # mail.acmeclient.com != acmeclient.com — exact match only, keeps rules predictable.
        config = make_config([CLIENT_DOMAIN_RULE])
        features = EmailFeatures.from_headers("someone@mail.acmeclient.com", "Hi", None)
        self.assertIsNone(classify_with_rules(features, config))

    def test_subject_contains_is_case_insensitive_substring(self):
        config = make_config([FINANCE_SUBJECT_RULE])
        features = EmailFeatures.from_headers("a@b.com", "Your INVOICE is ready", None)
        match = classify_with_rules(features, config)
        self.assertEqual(match.folder, "Finance")

    def test_subject_contains_matches_any_value_in_list(self):
        config = make_config([FINANCE_SUBJECT_RULE])
        features = EmailFeatures.from_headers("a@b.com", "Payment received, thanks!", None)
        self.assertIsNotNone(classify_with_rules(features, config))

    def test_sender_address_exact_match(self):
        config = make_config([VENDOR_EXACT_ADDRESS_RULE])
        features = EmailFeatures.from_headers("Stripe <billing@stripe.com>", "Receipt", None)
        match = classify_with_rules(features, config)
        self.assertEqual(match.folder, "Vendors")

    def test_sender_address_exact_match_rejects_different_local_part_same_domain(self):
        config = make_config([VENDOR_EXACT_ADDRESS_RULE])
        features = EmailFeatures.from_headers("support@stripe.com", "Hi", None)
        self.assertIsNone(classify_with_rules(features, config))

    def test_multiple_conditions_on_one_rule_are_and_ed(self):
        config = make_config([MULTI_CONDITION_RULE])
        # Domain matches but subject doesn't -> no match.
        features = EmailFeatures.from_headers("a@acmeclient.com", "Weekly update", None)
        self.assertIsNone(classify_with_rules(features, config))

        # Both match -> match.
        features2 = EmailFeatures.from_headers("a@acmeclient.com", "URGENT: server down", None)
        match = classify_with_rules(features2, config)
        self.assertEqual(match.urgency, "Urgent")

    def test_first_matching_rule_wins_in_order(self):
        # Newsletter rule listed first should win even though a later rule would
        # also match, mirroring "first match wins" semantics.
        config = make_config([NEWSLETTER_RULE, UNSUBSCRIBE_RULE])
        features = EmailFeatures.from_headers("noreply@example.com", "Hi", "<mailto:x@y.com>")
        match = classify_with_rules(features, config)
        self.assertEqual(match.rule_name, "Newsletter senders")

    def test_rule_with_no_conditions_never_matches(self):
        empty_rule = Rule(name="Empty", folder="Other", urgency="NoAction", conditions={})
        config = make_config([empty_rule])
        features = EmailFeatures.from_headers("a@b.com", "Hi", None)
        self.assertIsNone(classify_with_rules(features, config))

    def test_unknown_condition_key_raises(self):
        bad_rule = Rule(name="Bad", folder="Other", urgency="NoAction", conditions={"nonsense_key": ["x"]})
        config = make_config([bad_rule])
        features = EmailFeatures.from_headers("a@b.com", "Hi", None)
        with self.assertRaises(ValueError):
            classify_with_rules(features, config)


class TestUrgencyRank(unittest.TestCase):
    def test_lower_rank_is_more_urgent(self):
        config = make_config([])
        self.assertLess(config.urgency_rank("Urgent"), config.urgency_rank("Today"))
        self.assertLess(config.urgency_rank("Today"), config.urgency_rank("ThisWeek"))
        self.assertLess(config.urgency_rank("ThisWeek"), config.urgency_rank("NoAction"))

    def test_unknown_tier_ranks_as_least_urgent(self):
        config = make_config([])
        self.assertGreater(config.urgency_rank("MadeUpTier"), config.urgency_rank("NoAction"))


if __name__ == "__main__":
    unittest.main()
