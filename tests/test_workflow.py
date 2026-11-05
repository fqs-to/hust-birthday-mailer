import contextlib
import io
import os
import unittest
from unittest.mock import patch

from scripts.action_run import run


class WorkflowGateTests(unittest.TestCase):
    def test_cloud_email_test_also_checks_persistent_state(self):
        with patch.dict(os.environ, {"RUN_MODE": "test"}, clear=True), patch("scripts.action_run.main", return_value=0) as main:
            self.assertEqual(run(), 0)
            main.assert_called_once_with(["test-email", "--send", "--git-state"])

    def test_send_requires_explicit_enabled_flag(self):
        with patch.dict(os.environ, {"RUN_MODE": "send", "MEMBERS_CSV": "placeholder"}, clear=True):
            with patch("scripts.action_run.main") as main, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run(), 1)
                main.assert_not_called()

    def test_send_never_falls_back_to_sample_roster(self):
        with patch.dict(os.environ, {"RUN_MODE": "send", "BIRTHDAY_ENABLED": "true"}, clear=True):
            with patch("scripts.action_run.main") as main, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run(), 1)
                main.assert_not_called()

    def test_live_send_always_uses_durable_ledger(self):
        environment = {"RUN_MODE": "send", "BIRTHDAY_ENABLED": "true", "MEMBERS_CSV": "placeholder"}
        with patch.dict(os.environ, environment, clear=True), patch("scripts.action_run.main", return_value=0) as main:
            self.assertEqual(run(), 0)
            main.assert_called_once_with(["run", "--send", "--git-state"])

    def test_unknown_mode_is_rejected(self):
        with patch.dict(os.environ, {"RUN_MODE": "unexpected-mode"}, clear=True):
            with patch("scripts.action_run.main") as main, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run(), 1)
                main.assert_not_called()


if __name__ == "__main__":
    unittest.main()
