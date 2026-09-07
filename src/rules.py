from __future__ import annotations

from dataclasses import dataclass

from src.config import Config, Rule


@dataclass
class EmailFeatures:
    """Normalized fields a rule can match against. Built once per message."""

    sender_address: str  # lowercased, e.g. "billing@stripe.com"
    sender_local_part: str  # "billing"
    sender_domain: str  # "stripe.com"
    subject: str  # original case, for display
    subject_lower: str
    has_list_unsubscribe: bool

    @classmethod
    def from_headers(cls, from_header: str, subject: str, list_unsubscribe: str | None) -> "EmailFeatures":
        address = _extract_address(from_header).lower()
        local_part, _, domain = address.partition("@")
        return cls(
            sender_address=address,
            sender_local_part=local_part,
            sender_domain=domain,
            subject=subject or "",
            subject_lower=(subject or "").lower(),
            has_list_unsubscribe=bool(list_unsubscribe),
        )


def _extract_address(from_header: str) -> str:
    """Pull the bare email address out of a From header like 'Name <a@b.com>'."""
    if "<" in from_header and ">" in from_header:
        return from_header.split("<", 1)[1].split(">", 1)[0].strip()
    return from_header.strip()


@dataclass
class RuleMatch:
    rule_name: str
    folder: str
    urgency: str


def _condition_matches(features: EmailFeatures, key: str, value) -> bool:
    if key == "sender_domain":
        return features.sender_domain in {v.lower() for v in value}
    if key == "sender_address":
        return features.sender_address in {v.lower() for v in value}
    if key == "sender_local_part_prefix":
        return any(features.sender_local_part.startswith(v.lower()) for v in value)
    if key == "subject_contains":
        return any(v.lower() in features.subject_lower for v in value)
    if key == "has_list_unsubscribe":
        return features.has_list_unsubscribe == bool(value)
    raise ValueError(f"Unknown rule condition: {key}")


def _rule_matches(features: EmailFeatures, rule: Rule) -> bool:
    if not rule.conditions:
        return False
    return all(_condition_matches(features, key, value) for key, value in rule.conditions.items())


def classify_with_rules(features: EmailFeatures, config: Config) -> RuleMatch | None:
    """Return the first matching rule, or None if nothing matched (caller should
    fall back to the LLM classifier)."""
    for rule in config.rules:
        if _rule_matches(features, rule):
            return RuleMatch(rule_name=rule.name, folder=rule.folder, urgency=rule.urgency)
    return None
