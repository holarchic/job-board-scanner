"""Personio adapter: board URLs → feed URL, and a board's XML feed → postings.

A board is a subdomain of jobs.personio.de or jobs.personio.com; both hosts serve the same company, so either names
one board, read from .com. The feed is read in English: in its default language, German, an English-only job comes
without a description, while in English only a German-only job does. Parsing is all-or-nothing per board, as for the
others."""
from urllib.parse import quote
from xml.etree.ElementTree import Element

from jobscan.payload import board_slug, html_to_text, http_url, xml_root
from jobscan.records import Board, BoardError, Posting

ATS = "personio"
DISPLAY_NAME = "Personio"
DECODE = xml_root
DOMAINS = ("jobs.personio.de", "jobs.personio.com")
CRAWL_HOSTS = tuple(f"*.{domain}" for domain in DOMAINS)
FEED = "https://{slug}.jobs.personio.com/xml?language=en"
JOB = "https://{slug}.jobs.personio.com/job/{id}"


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


def parse(board: Board, root: Element) -> tuple[Posting, ...]:
    if root.tag != "workzag-jobs":
        raise BoardError("feed is not a Personio job list")
    # Some boards leave a job's name empty in every language: no title filter can match it, so it is skipped
    # rather than hiding the board's other jobs.
    postings = tuple(_posting(board, index, position) for index, position in enumerate(root.findall("position"))
                     if _text(position, "name"))
    if len({p.key for p in postings}) != len(postings):
        raise BoardError("duplicate job ids")
    return postings


def _posting(board: Board, index: int, position: Element) -> Posting:
    job_id, title = _text(position, "id"), _text(position, "name")
    if not job_id:
        raise BoardError(f"job {index}: no id")
    offices = [_text(position, "office")] + [_text(o, ".") for o in position.findall("additionalOffices/office")]
    return Posting(key=f"{board.id}:{job_id}", ats=ATS, board=board.id, company=board.company, title=title,
                   location=" / ".join(dict.fromkeys(o for o in offices if o)),
                   url=JOB.format(slug=quote(board.slug, safe=""), id=quote(job_id, safe="")),
                   description=_description(position))


def _description(position: Element) -> str:
    """Each section's heading, then its text."""
    sections = []
    for section in position.findall("jobDescriptions/jobDescription"):
        sections += [_text(section, "name"), html_to_text(_text(section, "value"))]
    return "\n".join(s for s in sections if s)


def _text(element: Element, path: str) -> str:
    return (element.findtext(path) or "").strip()
