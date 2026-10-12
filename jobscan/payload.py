"""Helpers for reading ATS data, shared by the adapters: board URLs, string fields that fail the board when
malformed, and job-description HTML → plain text."""
from html.parser import HTMLParser
from urllib.parse import SplitResult, urlsplit

from jobscan.records import BoardError

BLOCK_TAGS = {"br", "p", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6"}


def http_url(url: str) -> SplitResult | None:
    """An http(s) URL with a host, split; None for anything else. urlsplit, not urlparse, which would cut
    `;params` off a path segment."""
    try:
        parsed = urlsplit(url)
        parsed.port  # noqa: B018 -- raises on a malformed port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return parsed


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
    """Block tags become line breaks, other tags vanish, entities decode; blank lines and runs of spaces go. A real
    HTML parser, so a `>` inside a quoted attribute doesn't end the tag."""
    parser = _Text()
    parser.feed(markup)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)
