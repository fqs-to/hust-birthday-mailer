import json
import smtplib
import tempfile
import unittest
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import Mock, patch

from birthday_mailer.contacts import DataError, Member
from birthday_mailer.ledger import Ledger, ledger_lock
from birthday_mailer.mail import SMTPConfig, SMTPTransport, build_message, render_greeting
from birthday_mailer.service import deliver

SECRET = "test-key-" * 8
CONFIG = SMTPConfig("mail.example.edu", 465, "ssl", "sender@example.edu", "dummy-password", "sender@example.edu", "资委会")
MEMBER = Member("示例同学", "demo@example.edu", 11, 5)
DUE = [(MEMBER, "solar-2026")]


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "deliveries.json"

    def test_rerun_and_reloaded_ledger_do_not_resend(self):
        send = Mock()
        self.assertEqual(deliver(DUE, Ledger(self.path, SECRET), CONFIG, "资委会", send), 1)
        self.assertEqual(deliver(DUE, Ledger(self.path, SECRET), CONFIG, "资委会", send), 0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(Ledger(self.path, SECRET).status(MEMBER.email, "solar-2026"), "sent")

    def test_reservation_is_persisted_before_smtp(self):
        events = []
        ledger = Ledger(self.path, SECRET, lambda: events.append("sync"))
        def send(message):
            self.assertEqual(Ledger(self.path, SECRET).status(MEMBER.email, "solar-2026"), "pending")
            events.append("smtp")
        deliver(DUE, ledger, CONFIG, "资委会", send)
        self.assertEqual(events, ["sync", "smtp", "sync"])

    def test_failed_reservation_sync_sends_nothing(self):
        send = Mock()
        ledger = Ledger(self.path, SECRET, Mock(side_effect=DataError("sync failed")))
        with self.assertRaises(DataError):
            deliver(DUE, ledger, CONFIG, "资委会", send)
        send.assert_not_called()

    def test_ambiguous_smtp_failure_requires_manual_resolution(self):
        send = Mock(side_effect=smtplib.SMTPServerDisconnected("ambiguous"))
        with self.assertRaises(smtplib.SMTPServerDisconnected):
            deliver(DUE, Ledger(self.path, SECRET), CONFIG, "资委会", send)
        ledger = Ledger(self.path, SECRET)
        self.assertEqual(ledger.status(MEMBER.email, "solar-2026"), "uncertain")
        second = Mock()
        self.assertEqual(deliver(DUE, ledger, CONFIG, "资委会", second), 0)
        second.assert_not_called()
        ledger.resolve(MEMBER.email, "solar-2026", "allow-retry")
        self.assertEqual(deliver(DUE, ledger, CONFIG, "资委会", second), 1)

    def test_process_interruption_leaves_pending_record(self):
        with self.assertRaises(KeyboardInterrupt):
            deliver(DUE, Ledger(self.path, SECRET), CONFIG, "资委会", Mock(side_effect=KeyboardInterrupt))
        ledger = Ledger(self.path, SECRET)
        self.assertEqual(ledger.status(MEMBER.email, "solar-2026"), "pending")
        self.assertEqual(deliver(DUE, ledger, CONFIG, "资委会", Mock()), 0)

    def test_post_send_sync_failure_does_not_enable_a_retry(self):
        callback = Mock(side_effect=[None, DataError("sync failed")])
        send = Mock()
        with self.assertRaises(DataError):
            deliver(DUE, Ledger(self.path, SECRET, callback), CONFIG, "资委会", send)
        send.assert_called_once()
        self.assertEqual(deliver(DUE, Ledger(self.path, SECRET), CONFIG, "资委会", Mock()), 0)

    def test_next_birthday_cycle_can_send(self):
        ledger = Ledger(self.path, SECRET)
        send = Mock()
        deliver(DUE, ledger, CONFIG, "资委会", send)
        deliver([(MEMBER, "solar-2027")], ledger, CONFIG, "资委会", send)
        self.assertEqual(send.call_count, 2)

    def test_ledger_does_not_contain_personal_details(self):
        deliver(DUE, Ledger(self.path, SECRET), CONFIG, "资委会", Mock())
        text = self.path.read_text(encoding="utf-8")
        for value in [MEMBER.name, MEMBER.email, "birthday", SECRET]:
            self.assertNotIn(value, text)

    def test_changed_key_and_corrupt_state_fail_closed(self):
        deliver(DUE, Ledger(self.path, SECRET), CONFIG, "资委会", Mock())
        with self.assertRaises(DataError):
            Ledger(self.path, "different-key" * 5)
        self.path.write_text('{"version":1}', encoding="utf-8")
        with self.assertRaises(DataError):
            Ledger(self.path, SECRET)

    def test_concurrent_process_cannot_acquire_same_lock(self):
        with ledger_lock(self.path):
            with self.assertRaises(DataError):
                with ledger_lock(self.path):
                    self.fail("Second lock unexpectedly acquired")


class MailTests(unittest.TestCase):
    def test_display_name_changes_only_the_message_label(self):
        renamed = replace(CONFIG, sender_name="计算机学院资委会")
        message = build_message(MEMBER, renamed, "计算机学院资委会", "<test@example.edu>")
        self.assertEqual(message["From"].addresses[0].display_name, "计算机学院资委会")
        self.assertEqual(message["From"].addresses[0].addr_spec, CONFIG.sender)
        self.assertEqual(renamed.username, CONFIG.username)
        self.assertEqual(renamed.password, CONFIG.password)

    def test_letter_layout_single_recipient_and_unicode_body(self):
        member = Member("<示例&同学>", MEMBER.email, 11, 5)
        subject, text, markup = render_greeting(member, "<资委会>")
        message = build_message(member, CONFIG, "<资委会>", "<test@example.edu>")
        parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
        self.assertEqual(len(parsed.get_all("To")), 1)
        self.assertIsNone(parsed["Cc"])
        self.assertIsNone(parsed["Bcc"])
        self.assertEqual([part.get_content_type() for part in parsed.iter_parts()], ["text/plain", "text/html"])
        plain = parsed.get_body(preferencelist=("plain",))
        rendered = parsed.get_body(preferencelist=("html",))
        self.assertEqual(plain.get_content_charset(), "utf-8")
        self.assertEqual(rendered.get_content_charset(), "utf-8")
        self.assertEqual(parsed["Subject"], subject)
        self.assertEqual(plain.get_content(), text)
        self.assertEqual(rendered.get_content(), markup + "\n")
        self.assertIn("&lt;示例&amp;同学&gt;", markup)
        self.assertNotIn("<资委会>", markup)
        self.assertIn("border-top:1px solid #e1e5e9;color:#757575;font-size:12px", markup)
        self.assertLess(markup.index("font-size:16px"), markup.index("font-size:12px"))
        self.assertIn("亲爱的<示例&同学>同学：", text)
        self.assertIn("天天开心、无限进步！", text)
        self.assertEqual(parsed["Auto-Submitted"], "auto-generated")

    def test_ssl_transport_uses_verified_context_and_auth(self):
        with patch("birthday_mailer.mail.smtplib.SMTP_SSL") as factory:
            factory.return_value.send_message.return_value = {}
            with SMTPTransport(CONFIG) as transport:
                transport.send(build_message(MEMBER, CONFIG, "资委会", "<test@example.edu>"))
            kwargs = factory.call_args.kwargs
            self.assertTrue(kwargs["context"].check_hostname)
            factory.return_value.login.assert_called_once_with(CONFIG.username, CONFIG.password)

    def test_starttls_authenticates_after_encryption(self):
        config = SMTPConfig("mail.example.edu", 587, "starttls", CONFIG.username, CONFIG.password, CONFIG.sender, CONFIG.sender_name)
        with patch("birthday_mailer.mail.smtplib.SMTP") as factory:
            with SMTPTransport(config):
                pass
            calls = [call[0] for call in factory.return_value.method_calls]
            self.assertEqual(calls[:4], ["ehlo", "starttls", "ehlo", "login"])

    def test_quit_failure_does_not_undo_accepted_delivery(self):
        with patch("birthday_mailer.mail.smtplib.SMTP_SSL") as factory:
            factory.return_value.quit.side_effect = smtplib.SMTPServerDisconnected("after DATA")
            with SMTPTransport(CONFIG):
                pass
            factory.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
