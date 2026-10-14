"""Helpers for reading ATS data, shared by the adapters: board URLs and slugs, XML payloads, string fields that fail
the board when malformed, and job-description HTML → plain text."""
import html
import re
from html.parser import HTMLParser
from urllib.parse import SplitResult, unquote, urlsplit
from xml.etree import ElementTree

from jobscan.records import BoardError

BLOCK_TAGS = frozenset({"br", "p", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6"})
DECLARATION = re.compile(r"<![^>]*>")
ANY_TAG = re.compile(r"<[^>]*>")
# Board tokens include `ep+co` and `harrison&star`; ':' is the posting-key separator.
SLUG = re.compile(r"[!-~]+")
NOT_IN_SLUG = frozenset("/:?#")
REMOTE = re.compile(r"(?<!\w)remote(?!\w)")  # word-adjacent, as `prefilter` matches a `Remote` term


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


def board_slug(raw: str, decoded: bool = False) -> str | None:
    """A board's slug from a URL path segment (percent-decoded unless `decoded`), case kept; None unless it is
    printable ASCII free of `/:?#`. The adapter re-encodes it for its API URL."""
    slug = raw if decoded else unquote(raw)
    if not SLUG.fullmatch(slug) or NOT_IN_SLUG & set(slug):
        return None
    return slug


def xml_text(element: ElementTree.Element, path: str) -> str:
    """The text at `path` under `element`, stripped; "" when the element or its text is absent."""
    return (element.findtext(path) or "").strip()


def with_remote(places: list[str]) -> list[str]:
    """A remote job's places, plus `Remote` unless one of them already says remote as a word."""
    return places if any(REMOTE.search(p.casefold()) for p in places) else [*places, "Remote"]


def xml_root(body: bytes) -> ElementTree.Element:
    """An XML feed's root element, as an adapter's DECODE. ElementTree resolves no external entities; entity
    expansion is bounded only when Python links expat 2.4.1 or later (every build tested links 2.6 or later)."""
    try:
        return ElementTree.fromstring(body)
    # LookupError: an encoding Python doesn't know; ValueError: a multibyte encoding expat can't read.
    except (ElementTree.ParseError, LookupError, ValueError):
        raise BoardError("malformed XML") from None


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
    # Python 3.11's parser asserts on some malformed declarations, e.g. `<![bogus]>`: drop declarations and parse
    # again; only if that fails too, strip every tag.
    try:
        text = _parse(markup)
    except AssertionError:
        try:
            text = _parse(DECLARATION.sub(" ", markup))
        except AssertionError:
            text = html.unescape(ANY_TAG.sub(" ", markup))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _parse(markup: str) -> str:
    parser = _Text()
    parser.feed(markup)
    parser.close()
    return "".join(parser.parts)


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
