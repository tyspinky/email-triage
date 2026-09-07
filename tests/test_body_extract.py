import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.body_extract import extract_snippet, html_to_text, strip_quoted_and_signature


class TestHtmlToText(unittest.TestCase):
    def test_strips_tags(self):
        self.assertEqual(html_to_text("<p>Hello <b>world</b></p>"), "Hello world")

    def test_drops_script_and_style_content(self):
        html = "<style>.a{color:red}</style><p>Visible</p><script>evil()</script>"
        self.assertEqual(html_to_text(html), "Visible")

    def test_collapses_whitespace(self):
        html = "<p>Line one</p><p>Line two</p>"
        result = html_to_text(html)
        self.assertNotIn("   ", result)


class TestStripQuotedAndSignature(unittest.TestCase):
    def test_cuts_at_on_date_wrote(self):
        text = "Thanks, sounds good.\n\nOn Tue, Sep 2, 2026 at 10:03 AM Jane <jane@x.com> wrote:\n> original message"
        self.assertEqual(strip_quoted_and_signature(text), "Thanks, sounds good.")

    def test_cuts_at_original_message_marker(self):
        text = "See below.\n\n----- Original Message -----\nFrom: bob@x.com"
        self.assertEqual(strip_quoted_and_signature(text), "See below.")

    def test_cuts_at_signature_delimiter(self):
        text = "Sure, let's meet Friday.\n\n-- \nJohn Smith\nCEO, Example Inc."
        self.assertEqual(strip_quoted_and_signature(text), "Sure, let's meet Friday.")

    def test_no_marker_returns_full_text(self):
        text = "Just a plain short message with no quoting."
        self.assertEqual(strip_quoted_and_signature(text), text)

    def test_uses_earliest_cut_point_when_both_present(self):
        text = "Body text.\n\n-- \nSignature\n\nOn Mon wrote:\n> quoted"
        self.assertEqual(strip_quoted_and_signature(text), "Body text.")


class TestExtractSnippet(unittest.TestCase):
    def test_prefers_plain_text_over_html(self):
        result = extract_snippet(plain_text="Plain wins", html="<p>HTML loses</p>", max_chars=300)
        self.assertEqual(result, "Plain wins")

    def test_falls_back_to_html_when_no_plain_text(self):
        result = extract_snippet(plain_text=None, html="<p>Only HTML here</p>", max_chars=300)
        self.assertEqual(result, "Only HTML here")

    def test_falls_back_to_html_when_plain_text_is_blank(self):
        result = extract_snippet(plain_text="   ", html="<p>Fallback</p>", max_chars=300)
        self.assertEqual(result, "Fallback")

    def test_truncates_to_max_chars_with_ellipsis(self):
        long_text = "word " * 200
        result = extract_snippet(plain_text=long_text, html=None, max_chars=50)
        self.assertLessEqual(len(result), 51)  # 50 chars + ellipsis
        self.assertTrue(result.endswith("…"))

    def test_no_truncation_when_under_limit(self):
        result = extract_snippet(plain_text="short", html=None, max_chars=300)
        self.assertEqual(result, "short")

    def test_empty_input_returns_empty_string(self):
        self.assertEqual(extract_snippet(plain_text=None, html=None, max_chars=300), "")

    def test_strips_quoted_reply_before_truncating(self):
        text = "New content here.\n\nOn Mon, Jan 1, 2026 wrote:\n> " + ("old " * 200)
        result = extract_snippet(plain_text=text, html=None, max_chars=300)
        self.assertEqual(result, "New content here.")


if __name__ == "__main__":
    unittest.main()
