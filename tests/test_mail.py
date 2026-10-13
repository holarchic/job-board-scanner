"""Email delivery: the message built from a digest, and how it reaches the user's mail server."""
import smtplib
import ssl
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest import mock

from jobscan import mail
from jobscan.config import EmailConfig
from tests.fakes import PlainSMTPServer, patch_smtp

PASSWORD = "abcd-efgh-ijkl"
GMAIL = EmailConfig(to="you@example.com", sender="me@gmail.com", host="smtp.gmail.com", port=465, password=PASSWORD)
ICLOUD = EmailConfig(to="you@example.com", sender="me@icloud.com", host="smtp.mail.me.com", port=587,
                     password=PASSWORD)


class SmtpTestCase(unittest.TestCase):
    def setUp(self):
        self.calls, self.refuse = [], {}
        for patcher in patch_smtp(self.calls, self.refuse):
            patcher.start()
            self.addCleanup(patcher.stop)

    def names(self):
        return [name for name, _ in self.calls]

    def sent(self):
        """Each sent message as the recipient's client reads it: serialised for SMTP, parsed back."""
        return [BytesParser(policy=policy.default).parsebytes(args["message"].as_bytes(policy=policy.SMTP))
                for name, args in self.calls if name == "send"]


class SendTest(SmtpTestCase):
    def assert_verifying(self, context):
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual((context.check_hostname, context.verify_mode), (True, ssl.CERT_REQUIRED))

    def test_port_465_is_tls_from_the_start(self):
        mail.send_test(GMAIL, "pm")
        self.assertEqual(self.names(), ["SMTP_SSL", "login", "send", "quit"])
        connect = self.calls[0][1]
        self.assertEqual((connect["host"], connect["port"], connect["timeout"]), ("smtp.gmail.com", 465, 30))
        self.assert_verifying(connect["context"])
        self.assertEqual(self.calls[1][1], {"user": "me@gmail.com", "password": PASSWORD})

    def test_other_ports_start_tls_before_the_login(self):
        mail.send_test(ICLOUD, "pm")
        self.assertEqual(self.names(), ["SMTP", "starttls", "login", "send", "quit"])
        connect = self.calls[0][1]
        self.assertEqual((connect["host"], connect["port"], connect["timeout"]), ("smtp.mail.me.com", 587, 30))
        self.assert_verifying(self.calls[1][1]["context"])

    def test_every_failure_is_a_mail_error_with_secrets_masked(self):
        for email, fail in [
            (GMAIL, {"SMTP_SSL": ConnectionRefusedError(61, "Connection refused")}),
            (GMAIL, {"SMTP_SSL": TimeoutError("timed out")}),
            (GMAIL, {"SMTP_SSL": ssl.SSLCertVerificationError("certificate verify failed")}),
            (GMAIL, {"login": smtplib.SMTPAuthenticationError(535, f"5.7.8 {PASSWORD} not accepted".encode())}),
            (GMAIL, {"send": smtplib.SMTPRecipientsRefused({"you@example.com": (550, b"no such user")})}),
            (GMAIL, {"quit": smtplib.SMTPResponseException(421, b"closing")}),
            (ICLOUD, {"starttls": smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")}),
            (GMAIL, {"login": UnicodeEncodeError("ascii", PASSWORD, 0, 1, "ordinal not in range(128)")}),
        ]:
            self.refuse.clear()
            self.refuse.update(fail)
            with self.assertRaises(mail.MailError, msg=fail) as caught:
                mail.send_test(email, "pm")
            text = str(caught.exception)
            self.assertTrue(text, fail)
            for secret in (PASSWORD, "you@example.com", "me@gmail.com"):
                self.assertNotIn(secret, text)

    def test_a_reply_quoting_the_password_is_masked(self):
        self.refuse["login"] = smtplib.SMTPAuthenticationError(535, f"5.7.8 {PASSWORD} not accepted".encode())
        with self.assertRaises(mail.MailError) as caught:
            mail.send_test(GMAIL, "pm")
        self.assertIn("<password> not accepted", str(caught.exception))

    def test_tls_set_up_failing_is_a_mail_error(self):
        with mock.patch.object(mail.ssl, "create_default_context", side_effect=ssl.SSLError("no CA store")), \
                self.assertRaises(mail.MailError):
            mail.send_test(GMAIL, "pm")
        self.assertEqual(self.calls, [])

    def test_a_header_the_library_refuses_is_a_mail_error(self):
        with self.assertRaises(mail.MailError):
            mail.send_test(GMAIL, "pm\nBcc: x@example.com")
        self.assertEqual(self.calls, [])


class StartTlsRequiredTest(unittest.TestCase):
    def test_a_server_without_starttls_never_sees_the_password(self):
        """Through the real smtplib: the refusal is the library's, so no fake can make it pass."""
        server = PlainSMTPServer()
        self.addCleanup(server.close)
        email = EmailConfig(to="a@example.com", sender="b@example.com", host="127.0.0.1", port=server.port,
                            password=PASSWORD)
        with self.assertRaises(mail.MailError) as caught:
            mail.send_test(email, "pm")
        self.assertIn("SMTPNotSupportedError", str(caught.exception))
        self.assertTrue(any(line.upper().startswith("EHLO") for line in server.lines), server.lines)
        self.assertFalse(any(line.upper().startswith(("AUTH", "MAIL")) for line in server.lines), server.lines)


class DigestMessageTest(SmtpTestCase):
    def digest(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "2026-10-13.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_digest_file_arrives_as_written(self):
        text = ("# pm — 2026-10-13 · 2 new\n\nⓘ marks a tag that hasn't been validated.\n\n"
                "## Acme — Product Manager · London\n- founding: **yes** — \"our first PM\"\n" + "x" * 2000 + "\n")
        mail.send_digest(GMAIL, self.digest(text), "pm", 2, 0)
        (got,) = self.sent()
        self.assertEqual(got.get_content().replace("\r\n", "\n"), text)
        self.assertEqual((got["Subject"], got["From"], got["To"]),
                         ("jobscan: 2 new for pm", "me@gmail.com", "you@example.com"))
        self.assertTrue(got["Message-ID"].endswith("@gmail.com>"))
        self.assertIsNotNone(got["Date"])

    def test_subject_counts_untagged_postings(self):
        mail.send_digest(GMAIL, self.digest("# pm\n"), "pm", 0, 3)
        self.assertEqual(self.sent()[0]["Subject"], "jobscan: 0 new for pm · 3 untagged")

    def test_an_unreadable_digest_is_a_mail_error(self):
        with self.assertRaises(mail.MailError):
            mail.send_digest(GMAIL, self.digest("x").with_name("missing.md"), "pm", 1, 0)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
