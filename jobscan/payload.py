"""Helpers for reading ATS payloads, shared by the adapters: string fields that fail the board when malformed, and
job-description HTML → plain text."""
import html
import re

from jobscan.records import BoardError

BLOCK_TAG = re.compile(r"<\s*(?:br|/?p|/?li|/?div|/?h[1-6])\b[^>]*>", re.I)
ANY_TAG = re.compile(r"<[^>]+>")


def text_field(record: dict, name: str, index: int, required: bool = False) -> str | None:
    value = record.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise BoardError(f"job {index}: `{name}` is not {'a non-empty ' if required else 'a '}string")
    try:
        value.encode()  # a lone surrogate from a JSON escape can't be stored
    except UnicodeEncodeError:
        raise BoardError(f"job {index}: `{name}` is not valid Unicode") from None
    return value


def html_to_text(markup: str) -> str:
    """Block tags become line breaks, other tags vanish, entities decode; blank lines and runs of spaces go."""
    text = html.unescape(ANY_TAG.sub("", BLOCK_TAG.sub("\n", markup)))
    lines = (re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)
