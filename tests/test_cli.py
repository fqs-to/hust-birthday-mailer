import contextlib
import io
import json
import os
import smtplib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from birthday_mailer.cli import main
from birthday_mailer.contacts import DataError
from birthday_mailer.mail import BEIJING


class CLITests(unittest.TestCase):
    def setUp(self):
        # Unit tests must not consume a developer's real local mailbox config.
        local_env = patch("birthday_mailer.cli.load_env")
        local_env.start()
        self.addCleanup(local_env.stop)

    def invoke(self, arguments):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = main(arguments)
        return code, output.getvalue()

    def test_dry_run_has_no_smtp_or_personal_data_output(self):
        with patch.dict(os.environ, {"MEMBERS_CSV": "name,email,birthday\n示例,demo@example.edu,11-05\n"}, clear=True):
            with patch("birthday_mailer.cli.SMTPTransport") as transport:
                code, output = self.invoke(["run", "--date", "2026-11-05"])
        self.assertEqual(code, 0)
        self.assertIn("今日生日：1", output)
        self.assertNotIn("demo@example.edu", output)
        transport.assert_not_called()

    def test_simulated_date_cannot_be_used_for_real_send(self):
        code, output = self.invoke(["run", "--send", "--date", "2026-11-05"])
        self.assertEqual(code, 1)
        self.assertIn("仅可用于演练", output)

    def test_test_email_never_loads_or_sends_to_roster(self):
        environment = {"SMTP_USERNAME":"sender@example.edu", "SMTP_PASSWORD":"dummy"}
        with patch.dict(os.environ, environment, clear=True):
            with patch("birthday_mailer.cli.SMTPTransport") as factory:
                code, output = self.invoke(["test-email", "--send"])
                message = factory.return_value.__enter__.return_value.send.call_args.args[0]
        self.assertEqual(code, 0)
        self.assertEqual(message["To"].addresses[0].addr_spec, "sender@example.edu")
        self.assertIn("【测试邮件】", message["Subject"])

    def test_live_run_uses_member_name_and_normal_subject(self):
        today = datetime.now(BEIJING).date()
        environment = {
            "SMTP_USERNAME": "sender@example.edu",
            "SMTP_PASSWORD": "dummy",
            "STATE_SECRET": "test-state-key-" * 4,
            "MEMBERS_CSV": f"name,email,birthday\n张三,member@example.edu,{today:%m-%d}\n",
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, environment, clear=True):
                with patch("birthday_mailer.cli.SMTPTransport") as factory:
                    code, output = self.invoke(["run", "--send", "--ledger", str(Path(directory) / "deliveries.json")])
                    factory.return_value.__enter__.return_value.send.assert_called_once()
                    message = factory.return_value.__enter__.return_value.send.call_args.args[0]
        self.assertEqual(code, 0, output)
        self.assertEqual(message["To"].addresses[0].addr_spec, "member@example.edu")
        self.assertEqual(message["Subject"], "张三，生日快乐！｜计算机学院资委会")
        body = message.get_body(preferencelist=("plain",)).get_content()
        self.assertTrue(body.startswith("亲爱的张三同学：\n"))
        self.assertNotIn("测试同学", body)
        self.assertIn("华中科技大学计算机学院资委会\n\n\n────────────────────\n投递说明\n", body)

    def test_cloud_test_saves_empty_state_before_sending(self):
        environment = {"SMTP_USERNAME": "sender@example.edu", "SMTP_PASSWORD": "dummy", "STATE_SECRET": "test-state-key-" * 4}
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "deliveries.json"
            events = []
            def send(message):
                self.assertEqual(json.loads(ledger.read_text())["deliveries"], {})
                self.assertEqual(message["To"].addresses[0].addr_spec, "sender@example.edu")
                events.append("smtp")
            with patch.dict(os.environ, environment, clear=True), patch("birthday_mailer.cli.state_path", return_value=(ledger, lambda: events.append("sync"))):
                with patch("birthday_mailer.cli.SMTPTransport") as factory, patch("birthday_mailer.cli.load_members", side_effect=AssertionError("Roster must not be read")):
                    factory.return_value.__enter__.return_value.send.side_effect = send
                    code, output = self.invoke(["test-email", "--send", "--git-state"])
            self.assertEqual(code, 0, output)
            self.assertEqual(events, ["sync", "smtp"])

    def test_cloud_state_failure_prevents_test_email(self):
        environment = {"SMTP_USERNAME": "sender@example.edu", "SMTP_PASSWORD": "dummy", "STATE_SECRET": "test-state-key-" * 4}
        def failed_sync():
            raise DataError("state sync failed")
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, environment, clear=True), patch("birthday_mailer.cli.state_path", return_value=(Path(directory) / "state.json", failed_sync)):
                with patch("birthday_mailer.cli.SMTPTransport") as factory:
                    code, output = self.invoke(["test-email", "--send", "--git-state"])
            self.assertEqual(code, 1)
            factory.assert_not_called()

    def test_import_does_not_overwrite_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing.csv"
            target.write_text("existing", encoding="utf-8")
            code, output = self.invoke(["import", "does-not-exist.xlsx", "--output", str(target)])
            self.assertEqual(code, 1)
            self.assertEqual(target.read_text(), "existing")

    def test_preview_generates_text_without_smtp(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "preview.txt"
            with patch("birthday_mailer.cli.SMTPTransport") as transport:
                code, output = self.invoke(["preview", "--output", str(target)])
            self.assertEqual(code, 0)
            preview = target.read_text(encoding="utf-8")
            self.assertTrue(preview.startswith("亲爱的示例同学：\n\n您好！\n\n"))
            self.assertIn("生日快乐", preview)
            self.assertNotIn("<html", preview)
            self.assertIn("预览已保存", output)
            transport.assert_not_called()

    def test_chinese_output_works_in_non_utf8_pipe(self):
        sample = Path(__file__).resolve().parents[1] / "examples" / "members.csv"
        environment = {**os.environ, "PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"}
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-m", "birthday_mailer", "run", "--members", str(sample), "--date", "2026-11-05"],
                cwd=directory, env=environment, capture_output=True, encoding="utf-8", timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("北京时间日期", result.stdout)
        self.assertIn("发送邮件：0", result.stdout)

    def test_auth_error_shows_status_without_server_personal_data(self):
        environment = {"SMTP_USERNAME": "sender@example.edu", "SMTP_PASSWORD": "dummy-secret"}
        with patch.dict(os.environ, environment, clear=True):
            with patch("birthday_mailer.cli.SMTPTransport") as factory:
                factory.return_value.__enter__.side_effect = smtplib.SMTPAuthenticationError(535, b"server@example.edu dummy-secret")
                code, output = self.invoke(["test-email", "--send"])
        self.assertEqual(code, 1)
        self.assertIn("535", output)
        self.assertNotIn("server@example.edu", output)
        self.assertNotIn("dummy-secret", output)


if __name__ == "__main__":
    unittest.main()
