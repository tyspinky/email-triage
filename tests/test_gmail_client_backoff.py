import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from googleapiclient.errors import HttpError

from src.gmail_client import call_with_backoff


class _FakeResp:
    def __init__(self, status):
        self.status = status
        self.reason = "error"


def _http_error(status: int, reason: str = "") -> HttpError:
    content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()
    return HttpError(_FakeResp(status), content)


class TestCallWithBackoff(unittest.TestCase):
    def test_returns_result_on_first_success(self):
        calls = []

        def fn():
            calls.append(1)
            return "ok"

        with patch("time.sleep"):
            result = call_with_backoff(fn)
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 1)

    def test_retries_on_rate_limit_error_then_succeeds(self):
        attempts = {"n": 0}

        def fn():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise _http_error(403, "rateLimitExceeded")
            return "ok"

        with patch("time.sleep") as mock_sleep:
            result = call_with_backoff(fn)
        self.assertEqual(result, "ok")
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(mock_sleep.call_count, 2)

    def test_retries_on_user_rate_limit_exceeded(self):
        attempts = {"n": 0}

        def fn():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise _http_error(429, "userRateLimitExceeded")
            return "ok"

        with patch("time.sleep"):
            result = call_with_backoff(fn)
        self.assertEqual(result, "ok")

    def test_retries_on_5xx_regardless_of_reason(self):
        attempts = {"n": 0}

        def fn():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise _http_error(503, "")
            return "ok"

        with patch("time.sleep"):
            result = call_with_backoff(fn)
        self.assertEqual(result, "ok")

    def test_does_not_retry_on_non_retryable_403_reason(self):
        def fn():
            raise _http_error(403, "forbidden")

        with patch("time.sleep") as mock_sleep:
            with self.assertRaises(HttpError):
                call_with_backoff(fn)
        mock_sleep.assert_not_called()

    def test_does_not_retry_on_404(self):
        def fn():
            raise _http_error(404, "notFound")

        with patch("time.sleep") as mock_sleep:
            with self.assertRaises(HttpError):
                call_with_backoff(fn)
        mock_sleep.assert_not_called()

    def test_gives_up_after_max_attempts(self):
        attempts = {"n": 0}

        def fn():
            attempts["n"] += 1
            raise _http_error(429, "rateLimitExceeded")

        with patch("time.sleep") as mock_sleep:
            with self.assertRaises(HttpError):
                call_with_backoff(fn, max_attempts=3)
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(mock_sleep.call_count, 2)

    def test_backoff_delay_doubles_each_attempt(self):
        attempts = {"n": 0}

        def fn():
            attempts["n"] += 1
            if attempts["n"] < 4:
                raise _http_error(429, "rateLimitExceeded")
            return "ok"

        with patch("time.sleep") as mock_sleep:
            call_with_backoff(fn, base_delay=1.0)
        mock_sleep.assert_any_call(1.0)
        mock_sleep.assert_any_call(2.0)
        mock_sleep.assert_any_call(4.0)


if __name__ == "__main__":
    unittest.main()
