import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from birthday_mailer.cli import main


class CLITests(unittest.TestCase):
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

    def test_import_does_not_overwrite_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing.csv"
            target.write_text("existing", encoding="utf-8")
            code, output = self.invoke(["import", "does-not-exist.xlsx", "--output", str(target)])
            self.assertEqual(code, 1)
            self.assertEqual(target.read_text(), "existing")

    def test_preview_generates_html_without_smtp(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "preview.html"
            with patch("birthday_mailer.cli.SMTPTransport") as transport:
                code, output = self.invoke(["preview", "--output", str(target)])
            self.assertEqual(code, 0)
            self.assertIn("生日快乐", target.read_text(encoding="utf-8"))
            transport.assert_not_called()

    def test_chinese_output_works_in_non_utf8_pipe(self):
        sample = Path(__file__).resolve().parents[1] / "examples" / "members.csv"
        environment = {**os.environ, "PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"}
        result = subprocess.run(
            [sys.executable, "-m", "birthday_mailer", "run", "--members", str(sample), "--date", "2026-11-05"],
            env=environment, capture_output=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("北京时间日期", result.stdout)
        self.assertIn("发送邮件：0", result.stdout)


if __name__ == "__main__":
    unittest.main()
