"""Tests for the persistent worker loop's own resilience — the per-account
failure isolation itself is already covered by test_triage_integration.py;
this covers the outer loop wrapper that catches truly unexpected failures."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scheduler


class TestSchedulerLoop(unittest.TestCase):
    def test_survives_run_all_accounts_raising_and_sleeps_between_cycles(self):
        call_count = {"n": 0}

        def fake_run_all_accounts():
            call_count["n"] += 1
            if call_count["n"] >= 3:
                raise KeyboardInterrupt  # stop the infinite loop for the test
            raise RuntimeError("unexpected DB outage")

        with patch("scheduler.run_all_accounts", side_effect=fake_run_all_accounts), patch(
            "scheduler.time.sleep"
        ) as mock_sleep:
            with self.assertRaises(KeyboardInterrupt):
                scheduler.main()

        # Two RuntimeErrors were survived (the loop kept going), and it slept
        # between cycles rather than busy-looping.
        self.assertEqual(call_count["n"], 3)
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_called_with(scheduler.INTERVAL_SECONDS)

    def test_normal_cycle_sleeps_the_configured_interval(self):
        call_count = {"n": 0}

        def fake_run_all_accounts():
            call_count["n"] += 1
            if call_count["n"] >= 1:
                raise KeyboardInterrupt

        with patch("scheduler.run_all_accounts", side_effect=fake_run_all_accounts), patch(
            "scheduler.time.sleep"
        ) as mock_sleep:
            with self.assertRaises(KeyboardInterrupt):
                scheduler.main()

        mock_sleep.assert_not_called()  # KeyboardInterrupt isn't caught, loop exits before sleeping


if __name__ == "__main__":
    unittest.main()
