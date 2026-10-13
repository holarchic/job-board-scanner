"""Email delivery: the message built from a digest, and how it reaches the user's mail server."""
import smtplib
import ssl
import unittest
from email import policy
from email.parser import BytesParser
from unittest import mock

from jobscan import mail
from jobscan.config import EmailConfig
from tests.fakes import PlainSMTPServer, fake_smtp

PASSWORD = "abcd-efgh-ijkl"
GMAIL = EmailConfig(to="you@example.com", sender="me@gmail.com", host="smtp.gmail.com", port=465, password=PASSWORD)
ICLOUD = EmailConfig(to="you@example.com", sender="me@icloud.com", host="smtp.mail.me.com", port=587,
                     password=PASSWORD)


def received(message) -> object:
    """The message as the recipient's client reads it: serialised for SMTP, parsed back."""
    return BytesParser(policy=policy.default).parsebytes(message.as_bytes(policy=policy.SMTP))


class SendTest(unittest.TestCase):
    def send(self, email, fail=None):
        calls = []
        with mock.patch.object(mail.smtplib, "SMTP_SSL", fake_smtp(calls, fail)), \
                mock.patch.object(mail.smtplib, "SMTP", fake_smtp(calls, fail)):
            mail.send(email, mail.test_message(email, "pm"))
        return calls

    def assert_verifying(self, context):
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual((context.check_hostname, context.verify_mode), (True, ssl.CERT_REQUIRED))

    def test_port_465_is_tls_from_the_start(self):
        calls = self.send(GMAIL)
        self.assertEqual([name for name, _ in calls], ["connect", "login", "send", "quit"])
        connect = calls[0][1]
        self.assertEqual((connect["host"], connect["port"], connect["timeout"]), ("smtp.gmail.com", 465, 30))
        self.assert_verifying(connect["context"])
        self.assertEqual(calls[1][1], {"user": "me@gmail.com", "password": PASSWORD})

    def test_other_ports_start_tls_before_the_login(self):
        calls = self.send(ICLOUD)
        self.assertEqual([name for name, _ in calls], ["connect", "starttls", "login", "send", "quit"])
        self.assertEqual((calls[0][1]["port"], calls[0][1]["timeout"]), (587, 30))
        self.assert_verifying(calls[1][1]["context"])

    def test_a_server_without_starttls_never_sees_the_password(self):
        """Through the real smtplib: the refusal is the library's, so no fake can make it pass."""
        server = PlainSMTPServer()
        self.addCleanup(server.close)
        email = EmailConfig(to="a@example.com", sender="b@example.com", host="127.0.0.1", port=server.port,
                            password=PASSWORD)
        with self.assertRaises(mail.MailError) as caught:
            mail.send(email, mail.test_message(email, "pm"))
        self.assertIn("SMTPNotSupportedError", str(caught.exception))
        self.assertTrue(any(line.upper().startswith("EHLO") for line in server.lines), server.lines)
        self.assertFalse(any(line.upper().startswith(("AUTH", "MAIL")) for line in server.lines), server.lines)

    def test_every_failure_is_a_mail_error_without_the_password(self):
        for email, fail in [
            (GMAIL, {"connect": ConnectionRefusedError(61, "Connection refused")}),
            (GMAIL, {"connect": TimeoutError("timed out")}),
            (GMAIL, {"connect": ssl.SSLCertVerificationError("certificate verify failed")}),
            (GMAIL, {"login": smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")}),
            (GMAIL, {"send": smtplib.SMTPRecipientsRefused({"you@example.com": (550, b"no such user")})}),
            (ICLOUD, {"starttls": smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")}),
            (GMAIL, {"login": UnicodeEncodeError("ascii", PASSWORD, 0, 1, "ordinal not in range(128)")}),
        ]:
            with self.assertRaises(mail.MailError, msg=fail) as caught:
                self.send(email, fail)
            self.assertNotIn(PASSWORD, str(caught.exception))
            self.assertNotEqual(str(caught.exception), "", fail)


class MessageTest(unittest.TestCase):
    def test_digest_text_arrives_as_written(self):
        text = ("# pm — 2026-10-13 · 2 new\n\nⓘ marks a tag that hasn't been validated.\n\n"
                "## Acme — Product Manager · London\n- founding: **yes** — \"our first PM\"\n" + "x" * 2000 + "\n")
        got = received(mail.digest_message(GMAIL, "pm", 2, 0, text))
        self.assertEqual(got.get_content().replace("\r\n", "\n"), text)
        self.assertEqual((got["Subject"], got["From"], got["To"]),
                         ("jobscan: 2 new for pm", "me@gmail.com", "you@example.com"))
        self.assertTrue(got["Message-ID"].endswith("@gmail.com>"))
        self.assertIsNotNone(got["Date"])

    def test_subject_counts_untagged_postings(self):
        got = received(mail.digest_message(GMAIL, "pm", 0, 3, "# pm\n"))
        self.assertEqual(got["Subject"], "jobscan: 0 new for pm · 3 untagged")


if __name__ == "__main__":
    unittest.main()
