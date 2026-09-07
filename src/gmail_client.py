from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from googleapiclient.errors import HttpError

from src.body_extract import decode_base64url
from src.config import LabelColor

T = TypeVar("T")

_RETRYABLE_STATUSES = {403, 429, 500, 503}
_RETRYABLE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "backendError"}


def _is_retryable(exc: HttpError) -> bool:
    if exc.resp.status not in _RETRYABLE_STATUSES:
        return False
    if exc.resp.status in (500, 503):
        return True
    try:
        reason = json.loads(exc.content).get("error", {}).get("errors", [{}])[0].get("reason", "")
    except (ValueError, AttributeError, IndexError):
        return False
    return reason in _RETRYABLE_REASONS


def call_with_backoff(fn: Callable[[], T], *, max_attempts: int = 5, base_delay: float = 1.0) -> T:
    """Run a Gmail API call, retrying with exponential backoff on rate-limit /
    transient errors. Gmail's per-minute quota is easy to trip during a burst
    of messages, and without this a single 429/403 kills the whole run."""
    attempt = 0
    while True:
        try:
            return fn()
        except HttpError as exc:
            attempt += 1
            if attempt >= max_attempts or not _is_retryable(exc):
                raise
            time.sleep(base_delay * (2 ** (attempt - 1)))


@dataclass
class RawMessage:
    id: str
    thread_id: str
    from_header: str
    subject: str
    list_unsubscribe: str | None
    plain_text: str | None
    html: str | None
    label_ids: list[str] = field(default_factory=list)
    internal_date_ms: str | None = None  # epoch ms, as returned by Gmail


def list_candidate_message_ids(service, query: str) -> list[str]:
    ids = []
    page_token = None
    while True:
        resp = call_with_backoff(
            lambda: service.users()
            .messages()
            .list(userId="me", q=query, pageToken=page_token, maxResults=100)
            .execute()
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _header(headers: list[dict], name: str) -> str | None:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return None


def _walk_parts(part: dict, plain_chunks: list[str], html_chunks: list[str]) -> None:
    mime_type = part.get("mimeType", "")
    body = part.get("body", {})
    data = body.get("data")

    if data and mime_type == "text/plain":
        plain_chunks.append(decode_base64url(data))
    elif data and mime_type == "text/html":
        html_chunks.append(decode_base64url(data))

    for sub_part in part.get("parts", []) or []:
        _walk_parts(sub_part, plain_chunks, html_chunks)


def get_message(service, message_id: str) -> RawMessage:
    msg = call_with_backoff(
        lambda: service.users().messages().get(userId="me", id=message_id, format="full").execute()
    )
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])

    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    _walk_parts(payload, plain_chunks, html_chunks)

    return RawMessage(
        id=msg["id"],
        thread_id=msg["threadId"],
        from_header=_header(headers, "From") or "",
        subject=_header(headers, "Subject") or "",
        list_unsubscribe=_header(headers, "List-Unsubscribe"),
        plain_text="\n".join(plain_chunks) if plain_chunks else None,
        html="\n".join(html_chunks) if html_chunks else None,
        label_ids=msg.get("labelIds", []),
        internal_date_ms=msg.get("internalDate"),
    )


def _color_body(color: LabelColor) -> dict:
    return {"backgroundColor": color.background, "textColor": color.text}


class LabelManager:
    """Resolves label names to Gmail label IDs, creating them (nested under
    the configured prefix) on first use. Also applies/backfills label colors
    from config. Caches for the life of one run."""

    def __init__(self, service):
        self.service = service
        # name -> {"id": str, "color": {"backgroundColor", "textColor"} | None}
        self._cache: dict[str, dict] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        resp = call_with_backoff(lambda: self.service.users().labels().list(userId="me").execute())
        for label in resp.get("labels", []):
            self._cache[label["name"]] = {"id": label["id"], "color": label.get("color")}

    def get_or_create(self, name: str, color: LabelColor | None = None) -> str:
        if name not in self._cache:
            body = {
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            }
            if color:
                body["color"] = _color_body(color)
            created = call_with_backoff(
                lambda: self.service.users().labels().create(userId="me", body=body).execute()
            )
            self._cache[name] = {"id": created["id"], "color": created.get("color")}

        entry = self._cache[name]
        if color and entry["color"] != _color_body(color):
            updated = call_with_backoff(
                lambda: self.service.users()
                .labels()
                .patch(userId="me", id=entry["id"], body={"color": _color_body(color)})
                .execute()
            )
            entry["color"] = updated.get("color")

        return entry["id"]


def apply_labels(service, message_id: str, add_label_ids: list[str], remove_label_ids: list[str]) -> None:
    if not add_label_ids and not remove_label_ids:
        return
    call_with_backoff(
        lambda: service.users()
        .messages()
        .modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": add_label_ids, "removeLabelIds": remove_label_ids},
        )
        .execute()
    )
