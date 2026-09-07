from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Rule:
    name: str
    folder: str
    urgency: str
    conditions: dict[str, Any]


@dataclass
class LabelColor:
    background: str
    text: str


@dataclass
class Config:
    folders: list[str]
    default_folder: str
    urgency_tiers: list[str]
    default_urgency: str
    gmail_query: str
    label_prefix: str
    remove_from_inbox: bool
    llm_model: str
    llm_max_body_chars: int
    llm_enabled: bool
    rules: list[Rule]
    folder_colors: dict[str, LabelColor]
    urgency_colors: dict[str, LabelColor]

    def urgency_rank(self, tier: str) -> int:
        """Lower rank = more urgent. Unknown tiers sort as least urgent."""
        try:
            return self.urgency_tiers.index(tier)
        except ValueError:
            return len(self.urgency_tiers)


def load_config(path: str | Path | None = None) -> Config:
    load_dotenv(PROJECT_ROOT / ".env")

    config_path = Path(path) if path else PROJECT_ROOT / "config" / "config.yaml"
    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    rules = [
        Rule(
            name=r["name"],
            folder=r["folder"],
            urgency=r["urgency"],
            conditions=r.get("conditions", {}),
        )
        for r in raw.get("rules", [])
    ]

    gmail = raw.get("gmail", {})
    llm = raw.get("llm", {})

    def parse_colors(raw_colors: dict) -> dict[str, LabelColor]:
        return {
            name: LabelColor(background=c["background"], text=c["text"])
            for name, c in raw_colors.items()
        }

    return Config(
        folders=raw["folders"],
        default_folder=raw["default_folder"],
        urgency_tiers=raw["urgency_tiers"],
        default_urgency=raw["default_urgency"],
        gmail_query=gmail.get("query", "in:inbox"),
        label_prefix=gmail.get("label_prefix", "Triage"),
        remove_from_inbox=gmail.get("remove_from_inbox", False),
        llm_model=llm.get("model", "claude-haiku-4-5-20251001"),
        llm_max_body_chars=llm.get("max_body_chars", 300),
        llm_enabled=llm.get("enabled", True),
        rules=rules,
        folder_colors=parse_colors(raw.get("folder_colors", {})),
        urgency_colors=parse_colors(raw.get("urgency_colors", {})),
    )


def env(name: str, default: str | None = None) -> str:
    load_dotenv(PROJECT_ROOT / ".env")
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value
