from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from src.body_extract import extract_snippet
from src.config import Config, env, load_config
from src.dashboard import GMAIL_MESSAGE_URL, _display_sender
from src.gmail_client import RawMessage, get_message, list_candidate_message_ids

# Kept deliberately small — candidates and their snippets are what make up
# the bulk of the ranking prompt's token cost, and 25 candidates at ~120
# chars each is plenty to find 5-10 genuinely relevant emails.
DEFAULT_RESULT_COUNT = 8
DEFAULT_CANDIDATE_LIMIT = 25
_SNIPPET_CHARS_FOR_RANKING = 120

_KEYWORDS_TOOL = "extract_search_keywords"
_RANK_TOOL = "select_relevant_emails"


@dataclass
class Candidate:
    message_id: str
    subject: str
    sender: str
    snippet: str
    received_at: str  # ISO 8601, for display only


@dataclass
class RankedResult:
    message_id: str
    reason: str


def _received_at(msg: RawMessage) -> str:
    if not msg.internal_date_ms:
        return ""
    dt = datetime.fromtimestamp(int(msg.internal_date_ms) / 1000, tz=timezone.utc)
    return dt.isoformat()


def derive_search_keywords(user_query: str, config: Config) -> list[str]:
    """Turn a natural-language request into Gmail search keywords/phrases.
    Gmail search is keyword-based, not semantic, so this is the translation
    step between what the user asked for and what Gmail can actually match."""
    import anthropic

    client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model=config.llm_model,
        max_tokens=150,
        tools=[
            {
                "name": _KEYWORDS_TOOL,
                "description": "Extract Gmail search keywords/phrases for a natural-language email search request.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "3-6 short keywords or phrases likely to appear in the subject or "
                                "body of matching emails. A phrase may be multiple words."
                            ),
                        }
                    },
                    "required": ["keywords"],
                },
            }
        ],
        tool_choice={"type": "tool", "name": _KEYWORDS_TOOL},
        messages=[
            {
                "role": "user",
                "content": f"Request: {user_query!r}\n\nWhat Gmail search keywords/phrases would find matching emails?",
            }
        ],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == _KEYWORDS_TOOL:
            return [k.strip() for k in block.input.get("keywords", []) if k.strip()]
    return []


def build_gmail_query(keywords: list[str]) -> str:
    if not keywords:
        return ""
    parts = [f'"{k}"' if " " in k else k for k in keywords]
    return " OR ".join(parts)


def rank_candidates(user_query: str, candidates: list[Candidate], count: int, config: Config) -> list[RankedResult]:
    """Second LLM pass: from the keyword-matched candidates, pick the ones
    that are actually relevant to what the user asked, most relevant first."""
    import anthropic

    lines = [
        f"- id={c.message_id} subject={c.subject!r} from={_display_sender(c.sender)!r} snippet={c.snippet!r}"
        for c in candidates
    ]
    prompt = (
        f"The user asked: {user_query!r}\n\n"
        f"Below are candidate emails found via keyword search. Select up to {count} that are "
        "genuinely relevant, most relevant first, each with a one-sentence reason. If fewer than "
        f"{count} are truly relevant, return fewer — don't pad the list with weak matches.\n\n" + "\n".join(lines)
    )

    client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model=config.llm_model,
        max_tokens=500,
        tools=[
            {
                "name": _RANK_TOOL,
                "description": "Select and rank the most relevant emails for the user's request.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "results": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "message_id": {"type": "string"},
                                    "reason": {"type": "string"},
                                },
                                "required": ["message_id", "reason"],
                            },
                        }
                    },
                    "required": ["results"],
                },
            }
        ],
        tool_choice={"type": "tool", "name": _RANK_TOOL},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == _RANK_TOOL:
            return [
                RankedResult(message_id=r["message_id"], reason=r["reason"])
                for r in block.input.get("results", [])
                if r.get("message_id")
            ]
    return []


def run_search(
    query: str,
    *,
    service,
    config: Config,
    count: int = DEFAULT_RESULT_COUNT,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> list[dict]:
    keywords = derive_search_keywords(query, config)
    gmail_query = build_gmail_query(keywords)
    if not gmail_query:
        print("Could not work out search terms for that request.", file=sys.stderr)
        return []

    print(f"Searching Gmail for: {gmail_query}")
    message_ids = list_candidate_message_ids(service, gmail_query)[:candidate_limit]
    if not message_ids:
        return []

    candidates = []
    for message_id in message_ids:
        msg = get_message(service, message_id)
        snippet = extract_snippet(plain_text=msg.plain_text, html=msg.html, max_chars=_SNIPPET_CHARS_FOR_RANKING)
        candidates.append(
            Candidate(
                message_id=msg.id,
                subject=msg.subject,
                sender=msg.from_header,
                snippet=snippet,
                received_at=_received_at(msg),
            )
        )

    ranked = rank_candidates(query, candidates, count, config)
    by_id = {c.message_id: c for c in candidates}

    results = []
    for r in ranked:
        candidate = by_id.get(r.message_id)
        if candidate:
            results.append(
                {
                    "message_id": candidate.message_id,
                    "subject": candidate.subject,
                    "sender": _display_sender(candidate.sender),
                    "received_at": candidate.received_at,
                    "reason": r.reason,
                }
            )
    return results


def print_results(query: str, results: list[dict]) -> None:
    if not results:
        print(f"No relevant emails found for {query!r}.")
        return
    print(f"\nFound {len(results)} email(s) matching {query!r}:\n")
    for i, r in enumerate(results, 1):
        link = GMAIL_MESSAGE_URL.format(message_id=r["message_id"])
        print(f"{i}. {r['subject']}")
        print(f"   From: {_display_sender(r['sender'])}")
        print(f"   Why:  {r['reason']}")
        print(f"   {link}\n")


def main() -> None:
    """Local-dev/debugging helper — the real interface is the web app's
    /search route. Looks up an existing account by email and searches with
    its stored credentials, so you can debug a specific pilot's search
    results from a terminal without going through the browser."""
    from src.accounts import get_account_by_email
    from src.credentials_store import build_gmail_service_for_account
    from src.db import get_connection

    parser = argparse.ArgumentParser(description="Search a signed-up account's Gmail with a natural-language request.")
    parser.add_argument("account_email", help="The Google account email to search, e.g. user@example.com")
    parser.add_argument("query", help="What to search for, e.g. 'emails about business'")
    parser.add_argument("--count", type=int, default=DEFAULT_RESULT_COUNT, help="Max results to return (default 8)")
    args = parser.parse_args()

    conn = get_connection()
    account = get_account_by_email(conn, args.account_email)
    if not account:
        print(f"No account found for {args.account_email!r}.", file=sys.stderr)
        sys.exit(1)

    try:
        service = build_gmail_service_for_account(account)
        config = load_config()
        results = run_search(args.query, service=service, config=config, count=args.count)
    except Exception as exc:
        print(f"Search failed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()

    print_results(args.query, results)


if __name__ == "__main__":
    main()
