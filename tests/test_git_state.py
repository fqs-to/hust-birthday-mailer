import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from birthday_mailer import git_state
from birthday_mailer.contacts import DataError, Member
from birthday_mailer.ledger import Ledger
from birthday_mailer.mail import SMTPConfig
from birthday_mailer.service import deliver


@unittest.skipUnless(shutil.which("git"), "git is required for state integration tests")
class GitStateTests(unittest.TestCase):
    def command(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = self.root / "origin.git"
        self.project = self.root / "project"
        self.origin.mkdir()
        self.project.mkdir()
        self.command(self.origin, "init", "--bare")
        self.command(self.project, "init", "--initial-branch=main")
        self.command(self.project, "config", "user.name", "zxw")
        self.command(self.project, "config", "user.email", "test@example.edu")
        self.command(self.project, "config", "commit.gpgsign", "false")
        (self.project / "README.md").write_text("public source", encoding="utf-8")
        self.command(self.project, "add", "README.md")
        self.command(self.project, "commit", "-m", "Initial source")
        self.command(self.project, "remote", "add", "origin", str(self.origin))
        self.command(self.project, "push", "origin", "main")
        self.environment = patch.dict(os.environ, {"STATE_COMMIT_NAME":"zxw", "STATE_COMMIT_EMAIL":"test@example.edu"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_new_runner_restores_durable_state_and_skips_duplicate(self):
        state = self.project / ".state"
        git_state.prepare(self.project, state)
        ledger = Ledger(state / "deliveries.json", "state-test-" * 8, lambda: git_state.sync(state))
        member = Member("示例", "demo@example.edu", 11, 5)
        config = SMTPConfig("example.edu", 465, "ssl", "sender@example.edu", "dummy", "sender@example.edu", "资委会")
        send = Mock()
        deliver([(member, "solar-2026")], ledger, config, "资委会", send)
        persisted = self.command(self.origin, "show", "birthday-state:deliveries.json")
        self.assertEqual(next(iter(json.loads(persisted)["deliveries"].values()))["status"], "sent")
        self.assertNotIn(member.email, persisted)
        author = self.command(self.origin, "log", "birthday-state", "-1", "--format=%an|%cn")
        self.assertEqual(author, "zxw|zxw")
        fresh = self.root / "fresh"
        self.command(self.root, "clone", "--branch", "main", str(self.origin), str(fresh))
        fresh_state = fresh / ".state"
        git_state.prepare(fresh, fresh_state)
        new_ledger = Ledger(fresh_state / "deliveries.json", "state-test-" * 8, lambda: git_state.sync(fresh_state))
        self.assertEqual(deliver([(member, "solar-2026")], new_ledger, config, "资委会", send), 0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual((self.project / "README.md").read_text(), "public source")

    def test_remote_state_without_ledger_is_rejected(self):
        self.command(self.project, "push", "origin", "main:birthday-state")
        with self.assertRaises(DataError):
            git_state.prepare(self.project, self.project / ".state")


if __name__ == "__main__":
    unittest.main()
