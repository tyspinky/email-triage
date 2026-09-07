from __future__ import annotations

from dataclasses import dataclass

import anthropic

from src.config import Config, env

_TOOL_NAME = "classify_email"


@dataclass
class LLMClassification:
    folder: str
    urgency: str
    confidence: float


def _build_tool_schema(config: Config) -> dict:
    return {
        "name": _TOOL_NAME,
        "description": "Classify an email into a folder and an urgency tier.",
        "input_schema": {
            "type": "object",
            "properties": {
                "folder": {"type": "string", "enum": config.folders},
                "urgency": {"type": "string", "enum": config.urgency_tiers},
                "confidence": {
                    "type": "number",
                    "description": "Your confidence in this classification, 0.0 to 1.0.",
                },
            },
            "required": ["folder", "urgency", "confidence"],
        },
    }


def _build_prompt(*, sender: str, subject: str, snippet: str, config: Config) -> str:
    return f"""Classify this email.

Folders available: {", ".join(config.folders)}
Urgency tiers (most to least urgent): {", ".join(config.urgency_tiers)}

Rules for urgency: if you are not confident an email needs prompt attention,
choose a LOWER urgency tier (closer to "{config.urgency_tiers[-1]}"). Only use
the highest tier ("{config.urgency_tiers[0]}") when the email clearly demands
same-day action from a real person (not automated notifications, receipts, or
newsletters).

From: {sender}
Subject: {subject}
Body (truncated):
{snippet}
"""


def classify_email(*, sender: str, subject: str, snippet: str, config: Config) -> LLMClassification:
    client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))

    response = client.messages.create(
        model=config.llm_model,
        max_tokens=200,
        tools=[_build_tool_schema(config)],
        tool_choice={"type": "tool", "name": _TOOL_NAME},
        messages=[{"role": "user", "content": _build_prompt(sender=sender, subject=subject, snippet=snippet, config=config)}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == _TOOL_NAME:
            data = block.input
            folder = data["folder"] if data["folder"] in config.folders else config.default_folder
            urgency = data["urgency"] if data["urgency"] in config.urgency_tiers else config.default_urgency
            confidence = float(data.get("confidence", 0.0))
            return LLMClassification(folder=folder, urgency=urgency, confidence=confidence)

    # Model didn't return the tool call for some reason — fail safe to defaults
    # rather than raising and aborting the whole run over one message.
    return LLMClassification(folder=config.default_folder, urgency=config.default_urgency, confidence=0.0)
