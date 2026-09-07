from __future__ import annotations

import base64
import re
from html.parser import HTMLParser

# Matches the start of a quoted reply chain, e.g.:
#   "On Tue, Sep 2, 2026 at 10:03 AM Jane Doe <jane@example.com> wrote:"
# Deliberately loose: real-world clients vary in exact punctuation/wording.
_QUOTE_HEADER_RE = re.compile(r"^\s*On .{0,80} wrote:\s*$", re.MULTILINE)

# Common top-of-quote markers other than "On ... wrote:".
_QUOTE_MARKERS = [
    _QUOTE_HEADER_RE,
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*From:\s.+\nSent:\s.+\nTo:\s.+", re.MULTILINE),
]

# A conventional signature delimiter (RFC 3676): a line containing exactly "-- ".
_SIGNATURE_RE = re.compile(r"^-- \s*$", re.MULTILINE)


class _HTMLTextExtractor(HTMLParser):
    """Minimal HTML-to-text: drops tags/scripts/styles, keeps visible text."""

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag in ("br", "p", "div", "tr", "li"):
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    text = parser.text()
    # Collapse excess whitespace left behind by stripped tags.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def strip_quoted_and_signature(text: str) -> str:
    """Cut everything from the first quoted-reply marker or signature delimiter
    onward, so only the new content the sender actually wrote remains."""
    cut_points = []
    for pattern in _QUOTE_MARKERS:
        m = pattern.search(text)
        if m:
            cut_points.append(m.start())
    m = _SIGNATURE_RE.search(text)
    if m:
        cut_points.append(m.start())

    if cut_points:
        text = text[: min(cut_points)]
    return text.strip()


def decode_base64url(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def extract_snippet(*, plain_text: str | None, html: str | None, max_chars: int) -> str:
    """Produce the trimmed plain-text snippet sent to the LLM: prefer the
    message's plain-text part; fall back to stripping tags from HTML. Then
    remove quoted history/signatures and truncate."""
    if plain_text and plain_text.strip():
        text = plain_text
    elif html:
        text = html_to_text(html)
    else:
        text = ""

    text = strip_quoted_and_signature(text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text
