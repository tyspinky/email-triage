#!/usr/bin/env python3
"""Persistent worker: classifies every active account's mail every 5 minutes.
Deployed as a Render Background Worker (see render.yaml) — no HTTP port, just
an always-on loop. For a one-shot manual run instead, use run.py."""

from __future__ import annotations

import sys
import time
import traceback

from src.triage import run_all_accounts

INTERVAL_SECONDS = 300


def main() -> None:
    while True:
        try:
            run_all_accounts()
        except Exception:
            # Per-account failures are already isolated inside run_all_accounts;
            # this catches anything unexpected outside that (e.g. a DB outage)
            # so one bad cycle doesn't kill the whole worker process.
            print("Scheduler cycle failed unexpectedly:", file=sys.stderr)
            traceback.print_exc()
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
