"""Tests against the actual shipped config/config.yaml — catches ordering
mistakes and typos that isolated fixture-based tests can't see."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.rules import EmailFeatures, classify_with_rules


class TestRealConfig(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_security_folder_exists(self):
        self.assertIn("Security", self.config.folders)

    def test_security_rule_wins_over_newsletter_prefix_rule(self):
        # Google's security alerts come from a "no-reply"-style address, so the
        # Security rule must be listed before the generic newsletter-sender
        # rule or these would get miscategorized as low-priority newsletters.
        features = EmailFeatures.from_headers(
            "Google <no-reply@accounts.google.com>", "Security alert", None
        )
        match = classify_with_rules(features, self.config)
        self.assertIsNotNone(match)
        self.assertEqual(match.folder, "Security")
        self.assertEqual(match.urgency, "Today")

    def test_login_notification_matches_security_rule(self):
        features = EmailFeatures.from_headers(
            "Onelink <notifications@onelink.com>", "New login from iOS (Mobile Safari)", None
        )
        match = classify_with_rules(features, self.config)
        self.assertEqual(match.folder, "Security")

    def test_ordinary_newsletter_still_matches_newsletter_rule(self):
        features = EmailFeatures.from_headers(
            "no-reply@github.com", "Your build passed", None
        )
        match = classify_with_rules(features, self.config)
        self.assertEqual(match.folder, "Newsletters")

    def test_aliexpress_marketing_matches_without_needing_the_llm(self):
        # A high-frequency sender observed repeatedly falling through to the
        # LLM before this rule existed — worth a direct, free rule.
        features = EmailFeatures.from_headers(
            "AliExpress <aeug-preferences17@mail.aliexpress.com>", "ty.spink, you've got great taste", None
        )
        match = classify_with_rules(features, self.config)
        self.assertIsNotNone(match)
        self.assertEqual(match.folder, "Newsletters")
        self.assertEqual(match.urgency, "NoAction")

    def test_every_folder_has_a_valid_color(self):
        for folder in self.config.folders:
            color = self.config.folder_colors.get(folder)
            self.assertIsNotNone(color, f"missing folder_colors entry for {folder!r}")
            self.assertRegex(color.background, r"^#[0-9a-fA-F]{6}$")
            self.assertRegex(color.text, r"^#[0-9a-fA-F]{6}$")

    def test_every_urgency_tier_has_a_valid_color(self):
        for tier in self.config.urgency_tiers:
            color = self.config.urgency_colors.get(tier)
            self.assertIsNotNone(color, f"missing urgency_colors entry for {tier!r}")
            self.assertRegex(color.background, r"^#[0-9a-fA-F]{6}$")
            self.assertRegex(color.text, r"^#[0-9a-fA-F]{6}$")


if __name__ == "__main__":
    unittest.main()
