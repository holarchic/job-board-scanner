"""Helpers for reading ATS data, shared by the adapters: board URLs and slugs, XML payloads, string fields that fail
the board when malformed, job-description HTML → plain text, and company names from APIs and board pages."""
import html
import re
import string
from html.parser import HTMLParser
from types import MappingProxyType
from urllib.parse import SplitResult, unquote, urlsplit
from xml.etree import ElementTree

from jobscan.records import BoardError, Company

BLOCK_TAGS = frozenset({"br", "p", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6"})
DECLARATION = re.compile(r"<![^>]*>")
ANY_TAG = re.compile(r"<[^>]*>")
# Board tokens include `ep+co` and `harrison&star`; ':' is the posting-key separator.
SLUG = re.compile(r"[!-~]+")
NOT_IN_SLUG = frozenset("/:?#")
REMOTE = re.compile(r"(?<!\w)remote(?!\w)")  # word-adjacent, as `prefilter` matches a `Remote` term
# ASCII-only lower case: str.lower can change a string's length (`İ` → `i̇`), shifting offsets into the original.
ASCII_LOWER = MappingProxyType(str.maketrans(string.ascii_uppercase, string.ascii_lowercase))
MAX_PAY = 10**12  # above any real pay in any unit: a larger number is malformed, and every one allowed fits a float


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


def encodable(text: str) -> bool:
    """Text that can be stored and written as UTF-8: no lone surrogate (from a JSON escape or a terminal)."""
    try:
        text.encode()
    except UnicodeEncodeError:
        return False
    return True


def one_line(text: str) -> str:
    """`text` as one printable line: unprintable characters (zero-width spaces) dropped, any run of whitespace
    (newlines and non-breaking spaces included) one space."""
    return " ".join("".join(c for c in text if c.isprintable() or c.isspace()).split())


def pay_range(currency, low, high, unit: int = 1) -> str | None:
    """A job site's published pay range as one line, `USD 165,000 to 190,000` (one figure when the two are equal; two
    decimals on both when either isn't whole), the amounts given in 1/`unit` of the currency (cents: 100; the
    caller's positive constant). None when the currency is missing or blank, either amount isn't a number from 0 to
    `MAX_PAY`, or `low` exceeds `high`, so nothing a site sends raises: pay is optional, a malformed range is no pay,
    never a failed board."""
    code = one_line(currency) if isinstance(currency, str) and encodable(currency) else ""
    if not code or not (_amount(low) and _amount(high)) or low > high:
        return None
    low, high = abs(low) / unit, abs(high) / unit  # abs: -0.0 passes `0 <=` and would print as -0
    amounts = (low,) if low == high else (low, high)
    places = 0 if all(a.is_integer() for a in amounts) else 2
    return f"{code} " + " to ".join(f"{a:,.{places}f}" for a in amounts)


def _amount(value) -> bool:
    """A number pay can be: `bool` is an int to Python but never an amount, and NaN fails both comparisons."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= MAX_PAY


def named_company(name, website=None) -> Company:
    """A company as a digest heading can show it: the name with entities decoded, unprintable characters dropped and
    whitespace (newlines included) collapsed; a website only if it is an http(s) URL of printable characters without
    whitespace (else it could break a digest line, or fail to store). A name that leaves nothing is a BoardError: the
    board keeps its slug and is asked again another run."""
    text = one_line(html.unescape(name) if isinstance(name, str) else "")
    if not text:
        raise BoardError("no company name")
    site = website.strip() if isinstance(website, str) else ""
    usable = site.isprintable() and not any(c.isspace() for c in site) and http_url(site) is not None
    return Company(name=text, website=site if usable else None)


def element_text(page: str, opening: str, closing: str) -> str | None:
    """The raw text inside the first element whose start tag begins `opening` (`<title`) and which ends at `closing`
    (`</title>`), tag names matched case-insensitively; None when there is none. Plain searches, so a page full of
    unterminated tags still takes linear time."""
    lower = page.translate(ASCII_LOWER)
    start = lower.find(opening.translate(ASCII_LOWER))
    inside = lower.find(">", start) + 1 if start >= 0 else 0
    end = lower.find(closing.translate(ASCII_LOWER), inside) if inside else -1
    return page[inside:end] if end >= 0 else None


def html_title(page: bytes) -> str:
    """An HTML page's <title> text with whitespace collapsed; "" when it has none. A page that isn't UTF-8 raises
    ValueError."""
    return " ".join((element_text(page.decode(), "<title", "</title>") or "").split())


def xml_root(body: bytes) -> ElementTree.Element:
    """An XML feed's root element, or a BoardError. ElementTree resolves no external entities; entity
    expansion is bounded only when Python links expat 2.4.1 or later (every build tested links 2.6 or later)."""
    try:
        return ElementTree.fromstring(body)
    # LookupError: an encoding Python doesn't know; ValueError: a multibyte encoding expat can't read.
    except (ElementTree.ParseError, LookupError, ValueError):
        raise BoardError("malformed XML") from None


def detail_text(record: dict, name: str, where: str) -> str:
    """A string field of one posting's detail, "" when absent; a BoardError naming `where` when it is anything else
    or can't be stored."""
    value = record.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BoardError(f"detail {where}: `{name}` is not a string")
    try:
        value.encode()  # a lone surrogate from a JSON escape can't be stored
    except UnicodeEncodeError:
        raise BoardError(f"detail {where}: `{name}` is not valid Unicode") from None
    return value


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
