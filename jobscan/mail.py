"""Email delivery through the user's own mail account: the published digest's text as a plain-text message.

Every connection is encrypted before the login: TLS from the start on port 465, STARTTLS on any other port, and a
server that doesn't offer STARTTLS is refused rather than sent the password in the clear."""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from jobscan.config import EmailConfig

TIMEOUT = 30
TLS_PORT = 465


class MailError(Exception):
    """The message wasn't accepted for delivery; the text says why, never with the password."""


def digest_message(email: EmailConfig, profile: str, new: int, carried_over: int, text: str) -> EmailMessage:
    subject = f"jobscan: {new} new for {profile}" + (f" · {carried_over} untagged" if carried_over else "")
    return _message(email, subject, text)


def test_message(email: EmailConfig, profile: str) -> EmailMessage:
    return _message(email, f"jobscan: email set up for {profile}",
                    f"jobscan will email new postings for {profile} to this address.\n")


def _message(email: EmailConfig, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = email.sender
    message["To"] = email.to
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=email.sender.partition("@")[2])
    message.set_content(body)
    return message


def send(email: EmailConfig, message: EmailMessage) -> None:
    context = ssl.create_default_context()
    try:
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
    except (smtplib.SMTPException, OSError) as e:
        raise MailError(f"{type(e).__name__}: {e}") from None
