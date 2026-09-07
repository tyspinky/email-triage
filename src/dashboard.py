from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg2.extensions

from src.config import Config, env

# Gmail accepts an API message id directly in this URL fragment to deep-link
# straight to that message.
GMAIL_MESSAGE_URL = "https://mail.google.com/mail/u/0/#all/{message_id}"

# Matches "Display Name <address@x.com>", optionally quoted — used only to
# clean up how a sender is *displayed*; the raw header is still what's stored.
_SENDER_NAME_RE = re.compile(r'^\s*"?([^"<]*?)"?\s*<[^>]+>\s*$')


def _display_sender(raw_from_header: str) -> str:
    """A raw From header like '"Anthropic Ireland, Limited" <invoice+...@stripe.com>'
    is unreadable at a glance — show just the name, or the bare address if
    there's no name, never both."""
    match = _SENDER_NAME_RE.match(raw_from_header)
    if match and match.group(1).strip():
        return match.group(1).strip()
    if "<" in raw_from_header and ">" in raw_from_header:
        return raw_from_header.split("<", 1)[1].split(">", 1)[0].strip()
    return raw_from_header.strip()

# Regenerating the AI summary is the only part of this that costs API calls,
# so cap how much history goes into the todo list / summary.
DEFAULT_LOOKBACK_HOURS = 24
# The category browser is a "what's landed where" view, not a todo list, so
# it gets a much wider window — still bounded, so it doesn't grow unbounded
# as the audit log accumulates over months.
CATEGORY_LOOKBACK_HOURS = 24 * 30
_ACTIONABLE_TIERS = {"Urgent", "Today", "ThisWeek"}


@dataclass
class TodoItem:
    message_id: str
    sender: str
    subject: str
    folder: str
    urgency: str
    created_at: str
    snippet: str = ""


def fetch_recent_items(
    conn: psycopg2.extensions.connection, account_id: int, *, since_hours: int = DEFAULT_LOOKBACK_HOURS
) -> list[TodoItem]:
    """One row per message_id (its most recent classification), not one row
    per audit-log entry — a message can be logged more than once (dry-runs,
    re-processing) without that meaning it should show up twice. Scoped to
    one account — this is the tenant-isolation boundary for dashboard data."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    with conn.cursor() as cur:
        # DISTINCT ON (Postgres-specific): picks exactly one row per
        # message_id — the most recent one, per the inner ORDER BY — then the
        # outer query re-sorts those by recency for display.
        cur.execute(
            """SELECT * FROM (
                   SELECT DISTINCT ON (message_id)
                          message_id, sender, subject, folder, urgency, created_at, snippet
                   FROM classifications
                   WHERE account_id = %s AND created_at >= %s
                   ORDER BY message_id, created_at DESC
               ) AS latest
               ORDER BY created_at DESC""",
            (account_id, cutoff),
        )
        rows = cur.fetchall()
    return [
        TodoItem(
            message_id=row[0],
            sender=row[1],
            subject=row[2],
            folder=row[3],
            urgency=row[4],
            created_at=row[5].isoformat(),
            snippet=row[6],
        )
        for row in rows
    ]


def split_actionable(items: list[TodoItem], config: Config) -> tuple[list[TodoItem], int]:
    """Split into (actionable items, sorted most urgent first) and a count of
    everything else (NoAction / filed-away mail) that doesn't need a look."""
    actionable = [i for i in items if i.urgency in _ACTIONABLE_TIERS]
    actionable.sort(key=lambda i: config.urgency_rank(i.urgency))
    quiet_count = len(items) - len(actionable)
    return actionable, quiet_count


# Keeping the prompt small keeps this call cheap. Only the most urgent items
# get a full line — the rest are just counted, since the summary only needs
# to tell the user what to look at first, not restate every message.
_SUMMARY_DETAILED_ITEM_CAP = 12
_SUMMARY_SNIPPET_CHARS = 100


def build_summary_prompt(actionable: list[TodoItem]) -> str:
    detailed, overflow = actionable[:_SUMMARY_DETAILED_ITEM_CAP], actionable[_SUMMARY_DETAILED_ITEM_CAP:]
    lines = [
        f"- [{item.urgency}] {item.subject!r} from {_display_sender(item.sender)} ({item.folder}): "
        f"{item.snippet[:_SUMMARY_SNIPPET_CHARS]}"
        for item in detailed
    ]
    if overflow:
        lines.append(f"- (+{len(overflow)} more lower-priority item(s), not detailed here)")
    return (
        "You are triaging a personal inbox. Below is a list of emails that need some level "
        "of attention, most urgent first. Write a short, prioritized to-do list telling the "
        "person what to look at first and why. Plain text, a few short bullet points (max 6), "
        "group similar low-value items together, second person, no preamble.\n\n" + "\n".join(lines)
    )


def generate_llm_summary(actionable: list[TodoItem], config: Config) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model=config.llm_model,
        max_tokens=400,
        messages=[{"role": "user", "content": build_summary_prompt(actionable)}],
    )
    return "".join(block.text for block in response.content if block.type == "text").strip()


def _plain_fallback_summary(actionable: list[TodoItem]) -> str:
    if not actionable:
        return "Nothing needs your attention right now."
    lines = [
        f"[{item.urgency}] {item.subject} — {_display_sender(item.sender)}"
        + (f": {item.snippet[:120]}" if item.snippet else "")
        for item in actionable[:10]
    ]
    return "AI summary unavailable — showing the raw list instead:\n" + "\n".join(lines)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# Icons by rank (0 = most urgent), not by tier name — tier names are
# user-configurable, but their order in config.yaml is always most-to-least
# urgent, so ranking is the only stable thing to key off of.
_RANK_ICONS = ["🔴", "🟠", "🟡", "⚪"]


def _tier_icon(rank: int) -> str:
    return _RANK_ICONS[rank] if rank < len(_RANK_ICONS) else "⚪"


def _time_ago(iso_timestamp: str) -> str:
    try:
        then = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - then
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _group_by_urgency(actionable: list[TodoItem], config: Config) -> list[tuple[str, int, list[TodoItem]]]:
    """actionable is already sorted most-urgent-first; cluster consecutive
    same-tier items into (tier, rank, items) sections for the template."""
    sections: list[tuple[str, int, list[TodoItem]]] = []
    for item in actionable:
        rank = config.urgency_rank(item.urgency)
        if sections and sections[-1][0] == item.urgency:
            sections[-1][2].append(item)
        else:
            sections.append((item.urgency, rank, [item]))
    return sections


# Cap how many messages render inside one category's expandable list — some
# folders (Newsletters) can easily have 100+ items, and the page shouldn't
# get sluggish just to browse it.
_CATEGORY_ITEM_CAP = 25


def group_by_folder(items: list[TodoItem], config: Config) -> list[tuple[str, list[TodoItem]]]:
    """Every configured folder, in config.yaml order, each with its items
    (most-recent-first, since `items` already comes in that order) — folders
    with no mail yet still appear, so you can see what's empty at a glance."""
    by_folder: dict[str, list[TodoItem]] = {folder: [] for folder in config.folders}
    for item in items:
        by_folder.setdefault(item.folder, []).append(item)
    ordered = [(folder, by_folder[folder]) for folder in config.folders]
    leftover = [(folder, its) for folder, its in by_folder.items() if folder not in config.folders]
    return ordered + leftover


_SEARCH_SCRIPT = """<script>
function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : s;
  return div.innerHTML;
}

async function runInboxSearch() {
  const input = document.getElementById('search-input');
  const btn = document.getElementById('search-btn');
  const results = document.getElementById('search-results');
  const query = input.value.trim();
  if (!query) return;

  btn.disabled = true;
  results.innerHTML = '<div class="search-status">Searching… (this calls Claude twice and can take a few seconds)</div>';

  try {
    const resp = await fetch('/search?q=' + encodeURIComponent(query));
    const data = await resp.json();
    if (!resp.ok) {
      results.innerHTML = '<div class="search-status error">Search failed: ' + escapeHtml(data.error || 'unknown error') + '</div>';
      return;
    }
    if (!data.results.length) {
      results.innerHTML = '<div class="search-status">No relevant emails found for that.</div>';
      return;
    }
    results.innerHTML = data.results.map(r =>
      '<a class="item" href="https://mail.google.com/mail/u/0/#all/' + encodeURIComponent(r.message_id) + '" target="_blank" style="border-left-color:var(--accent)">' +
      '<div class="subject">' + escapeHtml(r.subject) + '</div>' +
      '<div class="sender">' + escapeHtml(r.sender) + '</div>' +
      '<div class="search-reason">' + escapeHtml(r.reason) + '</div>' +
      '</a>'
    ).join('');
  } catch (err) {
    results.innerHTML = '<div class="search-status error">Search failed: ' + escapeHtml(String(err)) + '</div>';
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('search-input').addEventListener('keydown', function (e) {
  if (e.key === 'Enter') runInboxSearch();
});
</script>"""


def render_dashboard_html(
    *,
    summary_text: str,
    actionable: list[TodoItem],
    quiet_count: int,
    generated_at: str,
    config: Config,
    all_items: list[TodoItem] | None = None,
) -> str:
    def color_for(urgency: str) -> tuple[str, str]:
        color = config.urgency_colors.get(urgency)
        return (color.background, color.text) if color else ("#cccccc", "#000000")

    def folder_color(folder: str) -> str:
        color = config.folder_colors.get(folder)
        return color.background if color else "#8b949e"

    def render_item(item: TodoItem, bg: str, text: str) -> str:
        link = GMAIL_MESSAGE_URL.format(message_id=item.message_id)
        snippet_html = f'<div class="snippet">{_escape(item.snippet[:200])}</div>' if item.snippet else ""
        return f"""<a class="item" href="{_escape(link)}" target="_blank" style="border-left-color:{bg}">
  <div class="item-top">
    <span class="folder">{_escape(item.folder)}</span>
    <span class="time-ago">{_escape(_time_ago(item.created_at))}</span>
  </div>
  <div class="subject">{_escape(item.subject)}</div>
  <div class="sender">{_escape(_display_sender(item.sender))}</div>
  {snippet_html}
</a>"""

    sections = _group_by_urgency(actionable, config)

    stat_pills = []
    for urgency, rank, items in sections:
        bg, text = color_for(urgency)
        stat_pills.append(
            f'<span class="stat-pill" style="background:{bg};color:{text}">'
            f"{_tier_icon(rank)} {len(items)} {_escape(urgency)}</span>"
        )
    stats_html = "".join(stat_pills)

    section_blocks = []
    for urgency, rank, items in sections:
        bg, text = color_for(urgency)
        item_html = "\n".join(render_item(item, bg, text) for item in items)
        section_blocks.append(
            f'<div class="section-title">{_tier_icon(rank)} {_escape(urgency)}</div>\n{item_html}'
        )

    if section_blocks:
        body_html = "\n".join(section_blocks)
    else:
        body_html = """<div class="empty-state">
  <div class="big">🎉</div>
  <div>You're all caught up — nothing needs attention right now.</div>
</div>"""

    categories = group_by_folder(all_items if all_items is not None else actionable, config)
    category_blocks = []
    for folder, folder_items in categories:
        dot = folder_color(folder)
        shown = folder_items[:_CATEGORY_ITEM_CAP]
        remaining = len(folder_items) - len(shown)

        if shown:
            rows = "\n".join(render_item(item, *color_for(item.urgency)) for item in shown)
            if remaining > 0:
                rows += f'\n<div class="cat-more">+{remaining} more not shown</div>'
        else:
            rows = '<div class="cat-empty">No messages</div>'

        category_blocks.append(
            f"""<details class="category">
  <summary><span class="cat-dot" style="background:{dot}"></span>{_escape(folder)}<span class="cat-count">{len(folder_items)}</span></summary>
  <div class="cat-body">{rows}</div>
</details>"""
        )
    categories_html = "\n".join(category_blocks)

    summary_html = _escape(summary_text).replace("\n", "<br>")

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='90'%3E%F0%9F%93%AC%3C/text%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<title>Inbox Triage</title>
<style>
  :root {{
    --bg: #f6f8fa; --fg: #1f2328; --card-bg: #ffffff; --border: #d0d7de; --muted: #656d76; --accent: #0969da;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #0d1117; --fg: #e6edf3; --card-bg: #161b22; --border: #30363d; --muted: #8b949e; --accent: #58a6ff; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--fg); margin: 0; padding: 2.5rem 1.25rem 3rem; -webkit-font-smoothing: antialiased; }}
  .container {{ max-width: 680px; margin: 0 auto; }}
  .header {{ display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem 1rem; margin-bottom: 1.5rem; }}
  .brand {{ display: flex; align-items: center; gap: 0.65rem; }}
  .brand-mark {{ width: 34px; height: 34px; border-radius: 9px; background: linear-gradient(135deg, var(--accent), #8e63ce); display: flex; align-items: center; justify-content: center; font-size: 1.05rem; flex-shrink: 0; }}
  .brand-text h1 {{ font-size: 1.25rem; font-weight: 700; margin: 0; letter-spacing: -0.01em; }}
  .brand-text .tagline {{ color: var(--muted); font-size: 0.78rem; margin-top: 0.05rem; }}
  .timestamp {{ color: var(--muted); font-size: 0.78rem; }}
  .stats {{ display: flex; gap: 0.5rem; margin: 1.1rem 0 1.5rem; flex-wrap: wrap; }}
  .stat-pill {{ font-size: 0.78rem; font-weight: 700; padding: 0.3rem 0.75rem; border-radius: 999px; }}
  .summary {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 1.1rem 1.3rem; margin-bottom: 1.75rem; line-height: 1.55; white-space: pre-wrap; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }}
  .section-title {{ font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); margin: 1.6rem 0 0.6rem; }}
  .section-title:first-of-type {{ margin-top: 0; }}
  .item {{ display: block; background: var(--card-bg); border: 1px solid var(--border); border-left: 4px solid var(--border); border-radius: 10px; padding: 0.85rem 1.1rem; margin-bottom: 0.55rem; text-decoration: none; color: inherit; transition: border-color .15s, transform .1s, box-shadow .15s; box-shadow: 0 1px 2px rgba(0,0,0,0.03); }}
  .item:hover {{ border-color: var(--accent); transform: translateX(2px); box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
  .item-top {{ display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }}
  .folder {{ color: var(--muted); font-size: 0.78rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.02em; }}
  .time-ago {{ color: var(--muted); font-size: 0.75rem; white-space: nowrap; }}
  .subject {{ font-weight: 600; margin-top: 0.4rem; font-size: 0.98rem; }}
  .sender {{ color: var(--muted); font-size: 0.82rem; margin-top: 0.1rem; }}
  .snippet, .search-reason {{ color: var(--fg); opacity: 0.75; font-size: 0.85rem; margin-top: 0.45rem; line-height: 1.45; }}
  .empty-state {{ text-align: center; padding: 3rem 1rem; color: var(--muted); }}
  .empty-state .big {{ font-size: 2.5rem; margin-bottom: 0.5rem; }}
  .quiet {{ color: var(--muted); font-size: 0.82rem; margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--border); }}
  h2 {{ font-size: 1.05rem; font-weight: 700; margin: 2rem 0 0.75rem; }}
  .category {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 0.5rem; overflow: hidden; box-shadow: 0 1px 2px rgba(0,0,0,0.03); }}
  .category summary {{ cursor: pointer; padding: 0.8rem 1.1rem; font-weight: 600; display: flex; align-items: center; gap: 0.6rem; list-style: none; }}
  .category summary::-webkit-details-marker {{ display: none; }}
  .category summary::before {{ content: ""; width: 6px; height: 6px; border-right: 1.5px solid var(--muted); border-bottom: 1.5px solid var(--muted); transform: rotate(-45deg); transition: transform .15s; margin-left: 1px; }}
  .category[open] summary::before {{ transform: rotate(45deg); margin-left: 0; margin-top: -2px; }}
  .cat-dot {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }}
  .cat-count {{ margin-left: auto; color: var(--muted); font-weight: 500; font-size: 0.85rem; }}
  .cat-body {{ padding: 0 0.75rem 0.75rem; }}
  .cat-body .item {{ margin: 0.4rem 0; }}
  .cat-more, .cat-empty {{ color: var(--muted); font-size: 0.82rem; padding: 0.4rem 0.35rem; }}
  .search-box {{ display: flex; gap: 0.5rem; margin: 0 0 1.5rem; background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 0.4rem; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
  .search-box input {{ flex: 1; font: inherit; font-size: 0.95rem; padding: 0.55rem 0.7rem; border-radius: 8px; border: none; background: transparent; color: var(--fg); }}
  .search-box input:focus {{ outline: none; }}
  .search-box input::placeholder {{ color: var(--muted); }}
  .search-box button {{ font: inherit; font-weight: 600; padding: 0.55rem 1.2rem; border-radius: 8px; border: none; background: var(--accent); color: #fff; cursor: pointer; transition: opacity .15s; }}
  .search-box button:hover {{ opacity: 0.9; }}
  .search-box button:disabled {{ opacity: 0.5; cursor: default; }}
  .search-status {{ color: var(--muted); font-size: 0.85rem; padding: 0.5rem 0.1rem 1rem; }}
  .search-status.error {{ color: #cf222e; }}
  @media (prefers-color-scheme: dark) {{ .search-status.error {{ color: #ff7b72; }} }}
  #search-results .item {{ margin-bottom: 0.55rem; }}
  .footer {{ text-align: center; color: var(--muted); font-size: 0.75rem; margin-top: 3rem; padding-top: 1.5rem; border-top: 1px solid var(--border); }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="brand">
      <div class="brand-mark">📬</div>
      <div class="brand-text">
        <h1>Inbox Triage</h1>
        <div class="tagline">Automated priority sorting for your inbox</div>
      </div>
    </div>
    <div class="timestamp">Updated {_escape(generated_at)} · refreshes every 5 min</div>
  </div>

  <div class="search-box">
    <input type="text" id="search-input" placeholder="Search your inbox — e.g. &quot;emails about business&quot;">
    <button id="search-btn" onclick="runInboxSearch()">Search</button>
  </div>
  <div id="search-results"></div>

  <div class="stats">{stats_html}</div>
  <div class="summary">{summary_html}</div>
  {body_html}
  <div class="quiet">📁 {quiet_count} newsletter/low-priority message(s) filed away, not shown here.</div>

  <h2>Browse by category</h2>
  <div class="categories">{categories_html}</div>

  <div class="footer">Inbox Triage · rules-first classification, AI only when needed</div>
</div>
{_SEARCH_SCRIPT}
</body>
</html>
"""


def render_dashboard_for_account(conn: psycopg2.extensions.connection, account_id: int, config: Config) -> str:
    """Renders the dashboard fresh on every request — no file to write, no
    staleness relative to the scheduler's 5-minute cycle. The only cached
    thing is the AI summary text itself (see below), stored per-account in
    Postgres rather than a local JSON file."""
    from src.accounts import get_summary_cache, save_summary_cache

    items = fetch_recent_items(conn, account_id)
    actionable, quiet_count = split_actionable(items, config)
    category_items = fetch_recent_items(conn, account_id, since_hours=CATEGORY_LOOKBACK_HOURS)

    # The AI summary is the only part of this that costs API calls. If the
    # exact same set of actionable messages is still actionable (nothing new
    # landed, nothing got resolved), there's nothing new to say — reuse the
    # last summary instead of paying for an identical one every page load.
    cached = get_summary_cache(conn, account_id)
    current_ids = sorted(item.message_id for item in actionable)

    if not actionable:
        summary_text = "Nothing needs your attention right now."
        save_summary_cache(conn, account_id, [], summary_text)
    elif cached and current_ids == cached[0]:
        summary_text = cached[1]
    else:
        try:
            summary_text = generate_llm_summary(actionable, config)
        except Exception:
            summary_text = _plain_fallback_summary(actionable)
        save_summary_cache(conn, account_id, current_ids, summary_text)

    return render_dashboard_html(
        summary_text=summary_text,
        actionable=actionable,
        quiet_count=quiet_count,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        config=config,
        all_items=category_items,
    )
