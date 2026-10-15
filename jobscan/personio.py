"""Personio adapter: board URLs → feed URL, and a board's XML feed → postings.

A board is a subdomain of jobs.personio.de or jobs.personio.com; both hosts serve the same company, so either names
one board, read from .com. The feed is read in English, and again in its default language when some job has no
English text: the English feed empties a German-only job's description, the default feed an English-only job's.
Parsing is all-or-nothing per board, as for the others, except that a job with no name is skipped (and closed, if it
was open): some boards leave one job's name empty in every language, no title filter can match it, and failing the
board would hide the rest."""
from urllib.parse import quote
from xml.etree.ElementTree import Element

from jobscan.payload import board_slug, html_to_text, http_url, xml_root, xml_text
from jobscan.records import Board, BoardError, Get, Posting

ATS = "personio"
DISPLAY_NAME = "Personio"
describe = None  # the list carries each job's description
HOST = "jobs.personio.com"  # where every board is read, whichever host named it
DOMAINS = ("jobs.personio.de", HOST)
CRAWL_HOSTS = tuple(f"*.{domain}" for domain in DOMAINS)
ENGLISH = "?language=en"
FEED = "https://{slug}." + HOST + "/xml" + ENGLISH
JOB = "https://{slug}." + HOST + "/job/{id}"


def api_board(company: str, url: str) -> Board | None:
    """None: the feed lives on the board's own host, which page_board reads, and `/xml` on any other host names no
    Personio board. (`fetch.board_for` tries every api_board first.)"""
    return None


def page_board(company: str, url: str) -> Board | None:
    """Any URL on <slug>.jobs.personio.de or .com. Host names are case-insensitive, so the slug is lower case."""
    parsed = http_url(url)
    labels = parsed.hostname.split(".") if parsed else []
    if len(labels) != 4 or ".".join(labels[1:]) not in DOMAINS:
        return None
    slug = board_slug(labels[0], decoded=True)
    if slug is None:
        return None
    return Board(ats=ATS, slug=slug, company=company or slug, api_url=FEED.format(slug=quote(slug, safe="")))


def read(board: Board, get: Get) -> tuple[Posting, ...]:
    english = xml_root(get(board.api_url))
    if all(_has_text(p) for p in english.findall("position") if xml_text(p, "name")):
        return parse(board, english)
    return parse(board, english, xml_root(get(board.api_url.removesuffix(ENGLISH))))


def parse(board: Board, root: Element, default: Element | None = None) -> tuple[Posting, ...]:
    """`root` is the English feed; a job with no text there takes its sections from `default`'s job of the same id,
    when that one has text. The two feeds must list the same jobs, named or not: a job added between the two
    requests would otherwise pair descriptions wrongly, so the board fails for this run."""
    positions = _positions(root)
    fallback = {} if default is None else _positions(default)
    if default is not None and fallback.keys() != positions.keys():
        raise BoardError("feeds disagree: the English and default-language feeds list different jobs")
    return tuple(_posting(board, job_id, position, _described(position, fallback.get(job_id)))
                 for job_id, position in positions.items() if xml_text(position, "name"))


def _positions(root: Element) -> dict[str, Element]:
    """Every job by id, named or not, in feed order."""
    if root.tag != "workzag-jobs":
        raise BoardError("feed is not a Personio job list")
    positions = root.findall("position")
    ids = [xml_text(position, "id") for position in positions]
    if "" in ids:
        raise BoardError(f"job {ids.index('')}: no id")
    if len(set(ids)) != len(ids):
        raise BoardError("duplicate job ids")
    return dict(zip(ids, positions, strict=True))


def _has_text(position: Element) -> bool:
    """Some section holds text: headings alone, or markup with no text, don't count."""
    sections = position.findall("jobDescriptions/jobDescription")
    return any(html_to_text(xml_text(section, "value")) for section in sections)


def _described(english: Element, default: Element | None) -> Element:
    """The job whose sections describe it: the English one, unless only the default language's has text."""
    return default if default is not None and not _has_text(english) and _has_text(default) else english


def _posting(board: Board, job_id: str, position: Element, described: Element) -> Posting:
    title = xml_text(position, "name")
    offices = [xml_text(position, "office")] + [xml_text(o, ".") for o in position.findall("additionalOffices/office")]
    return Posting(key=f"{board.id}:{job_id}", ats=ATS, board=board.id, company=board.company, title=title,
                   location=" / ".join(dict.fromkeys(o for o in offices if o)),
                   url=JOB.format(slug=quote(board.slug, safe=""), id=quote(job_id, safe="")),
                   description=_description(described))


def _description(position: Element) -> str:
    """Each section's heading, then its text."""
    sections = []
    for section in position.findall("jobDescriptions/jobDescription"):
        sections += [xml_text(section, "name"), html_to_text(xml_text(section, "value"))]
    return "\n".join(s for s in sections if s)
