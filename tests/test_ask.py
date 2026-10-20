"""Unit tests for ask.py. Mock the service; no real network or disk state."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import ask


class QuestionTests(unittest.TestCase):
    def test_question_positional(self):
        self.assertEqual(ask.ask_question(["hello"]), "hello")

    def test_question_strips_whitespace(self):
        self.assertEqual(ask.ask_question(["   q   "]), "q")

    def test_question_long(self):
        with self.assertRaises(SystemExit):
            ask.ask_question(["a" * 3000])

    @patch("sys.stdin")
    def test_question_empty_stdin_exits(self, stdin):
        stdin.isatty.return_value = False
        stdin.read.return_value = "   "
        with self.assertRaises(SystemExit):
            ask.ask_question([])

    @patch("sys.stdin")
    def test_question_stdin(self, stdin):
        stdin.isatty.return_value = False
        stdin.read.return_value = "  a real question\n"
        self.assertEqual(ask.ask_question([]), "a real question")


class ProviderTests(unittest.TestCase):
    @patch("shutil.which")
    def test_codex_preferred(self, which):
        which.side_effect = lambda n: n + "/" if n == "codex" else None
        self.assertEqual(ask.choose_provider(), "codex")

    @patch("shutil.which")
    def test_claude_fallback(self, which):
        which.side_effect = lambda n: "/c/" if n == "claude" else None
        self.assertEqual(ask.choose_provider(), "claude")

    @patch("shutil.which")
    def test_none(self, which):
        which.return_value = None
        self.assertIsNone(ask.choose_provider())


class OneLineTests(unittest.TestCase):
    def test_truncation(self):
        self.assertEqual(
            ask.one_line("multi\nline error\n"), "multi line error")

    def test_short(self):
        self.assertEqual(ask.one_line("short"), "short")

    def test_empty(self):
        self.assertEqual(ask.one_line(""), "the service hit an issue.")


class NewTaskTests(unittest.TestCase):
    def test_new_task_submits(self):
        with tempfile.TemporaryDirectory() as d:
            dpath = type(ask.STATE_DIR)(d)
            (dpath / "device.json").write_text(
                json.dumps({"device_id": "dev1"}))
            with patch("ask.STATE_DIR", dpath):
                with patch("ask._fetch", return_value=b'{"task_id": "t1"}'):
                    tid = ask.new_task("What is x?", "codex")
            self.assertEqual(tid, "t1")


class WaitForReportTests(unittest.TestCase):
    @patch("time.sleep")
    @patch("ask._fetch")
    def test_completed_returns_data(self, fetch, sleep):
        data = {"state": "completed", "result": {"text": "done"},
                "events": []}
        fetch.return_value = json.dumps(data).encode("utf-8")
        self.assertEqual(ask.wait_for_report("t1")["state"], "completed")

    @patch("time.sleep")
    @patch("ask._fetch")
    def test_progress_labels_printed(self, fetch, sleep):
        data = {"state": "completed", "result": {},
                "events": [{"kind": "progress", "message": "ok"}]}
        fetch.return_value = json.dumps(data).encode("utf-8")
        ask.wait_for_report("t1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
