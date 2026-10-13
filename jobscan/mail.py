"""Email delivery through the user's own mail account: the published digest's text as a plain-text message.

Every connection is encrypted before the login: TLS from the start on port 465, STARTTLS on any other port, and a
server that doesn't offer STARTTLS is refused rather than sent the password in the clear."""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from jobscan.config import EmailConfig

TIMEOUT = 30
TLS_PORT = 465


class MailError(Exception):
    """The message wasn't accepted for delivery. The text says why, with the password and the addresses masked:
    a server's reply can quote what it was sent."""


def send_digest(email: EmailConfig, digest: Path, profile: str, new: int, carried_over: int) -> None:
    """Emails the published digest file as it is on disk."""
    try:
        text = digest.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise MailError(f"could not read {digest} ({type(e).__name__})") from None
    subject = f"jobscan: {new} new for {profile}" + (f" · {carried_over} untagged" if carried_over else "")
    send(email, subject, text)


def send_test(email: EmailConfig, profile: str) -> None:
    send(email, f"jobscan: email set up for {profile}",
         f"jobscan will email new postings for {profile} to this address.\n")


def send(email: EmailConfig, subject: str, body: str) -> None:
    try:
        message = _message(email, subject, body)
        context = ssl.create_default_context()
        if email.port == TLS_PORT:
            server = smtplib.SMTP_SSL(email.host, email.port, timeout=TIMEOUT, context=context)
        else:
            server = smtplib.SMTP(email.host, email.port, timeout=TIMEOUT)
        with server:
            if email.port != TLS_PORT:
                server.starttls(context=context)  # SMTPNotSupportedError when not offered: no login follows
            server.login(email.sender, email.password)
            server.send_message(message)
    except UnicodeError as e:  # its text quotes the offending characters
        raise MailError(type(e).__name__) from None
    except (smtplib.SMTPException, OSError, ValueError) as e:  # ValueError: a header the library won't write
        raise MailError(_masked(f"{type(e).__name__}: {e}", email)) from None


def _message(email: EmailConfig, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = email.sender
    message["To"] = email.to
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=email.sender.partition("@")[2])
    message.set_content(body)
    return message


def _masked(reason: str, email: EmailConfig) -> str:
    for secret, mask in [(email.password, "<password>"), (email.to, "<to>"), (email.sender, "<from>")]:
        reason = reason.replace(secret, mask)
    return reason
