"""Greenhouse adapter: board URLs → API URL, and the public Job Board API payload → postings.

One API host serves US and EU boards. Parsing is all-or-nothing per board, as for Ashby."""
import html
import json
import re
from urllib.parse import parse_qs, quote, unquote

from jobscan.payload import board_slug, html_to_text, http_url, named_company, text_field
from jobscan.records import Board, BoardError, Company, Get, Posting

ATS = "greenhouse"
DISPLAY_NAME = "Greenhouse"
describe = None  # the list carries each job's description
# boards.greenhouse.io redirects to these, so Common Crawl holds no status-200 pages of its own.
CRAWL_HOSTS = ("job-boards.greenhouse.io", "job-boards.eu.greenhouse.io")
BOARD_HOSTS = frozenset({*CRAWL_HOSTS, "boards.greenhouse.io", "boards.eu.greenhouse.io"})
API = "https://boards-api.greenhouse.io/v1/boards/"
LIST = "/jobs?content=true"
REAL_TAG = re.compile(r"</?[a-zA-Z]")
# A layer of escaped markup shows an escaped closing tag (any tag) or an escaped standard HTML tag (`<br>`, an
# unclosed `<p>`); a bare `&lt;customer&gt;` is neither and stays text.
ESCAPED_MARKUP = re.compile(r"&(?:amp;)?lt;(?:/[a-z]|(?:a|article|b|blockquote|br|code|dd|div|dl|dt|em|font|h[1-6]|"
                            r"hr|i|img|li|mark|ol|p|pre|s|section|small|span|strong|sub|sup|table|tbody|td|th|thead|"
                            r"tr|u|ul|wbr)\b)", re.IGNORECASE)


# A token is percent-decoded and case-folded (Greenhouse tokens are case-insensitive), so every spelling of a board
# is one board; the API URL encodes it again. `fetch.board_for` tries every adapter's api_board before any page_board.


def api_board(company: str, url: str) -> Board | None:
    """A Job Board API URL on any host (how tests point at a fake server); the host is kept."""
    parsed = http_url(url)
    path = [p for p in parsed.path.split("/") if p] if parsed else []
    if path[:2] != ["v1", "boards"] or len(path) < 3:
        return None
    return _board(company, _slug(path[2]), f"{parsed.scheme}://{parsed.netloc}/v1/boards/")


def page_board(company: str, url: str) -> Board | None:
    """A board page on a Greenhouse host, current or legacy, including the embed form `…/embed/job_board?for=<slug>`."""
    parsed = http_url(url)
    if parsed is None or parsed.hostname not in BOARD_HOSTS:
        return None
    path = [p for p in parsed.path.split("/") if p]
    if path[:2] == ["embed", "job_board"]:
        return _board(company, _slug(parse_qs(parsed.query).get("for", [""])[0], decoded=True), API)
    if not path or path[0] == "embed":
        return None
    return _board(company, _slug(path[0]), API)


def _board(company: str, slug: str | None, base: str) -> Board | None:
    if slug is None:
        return None
    return Board(ats=ATS, slug=slug, company=company or slug, api_url=base + quote(slug, safe="") + LIST)


def _slug(raw: str, decoded: bool = False) -> str | None:
    """Case-folded before the check: `%E2%84%AA` (the Kelvin sign) folds to `k`."""
    return board_slug((raw if decoded else unquote(raw)).lower(), decoded=True)


def company(board: Board, get: Get) -> Company:
    """The board's own record in the API, which names it."""
    data = json.loads(get(board.api_url.removesuffix(LIST)))
    return named_company(data.get("name") if isinstance(data, dict) else None)


def read(board: Board, get: Get) -> tuple[Posting, ...]:
    return parse(board, json.loads(get(board.api_url)))


def parse(board: Board, data) -> tuple[Posting, ...]:
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise BoardError("response has no `jobs` list")
    postings = tuple(_posting(board, i, job) for i, job in enumerate(jobs))
    if len({p.key for p in postings}) != len(postings):
        raise BoardError("duplicate job ids")
    return postings


def _posting(board: Board, index: int, job) -> Posting:
    if not isinstance(job, dict):
        raise BoardError(f"job {index} is not an object")
    job_id = job.get("id")
    if isinstance(job_id, int) and not isinstance(job_id, bool):
        job_id = str(job_id)
    elif not isinstance(job_id, str):
        raise BoardError(f"job {index}: `id` is not a number or a non-empty string")
    else:
        job_id = text_field(job, "id", index, required=True)
    return Posting(
        key=f"{board.id}:{job_id}",
        ats=ATS,
        board=board.id,
        company=board.company,
        title=text_field(job, "title", index, required=True).strip(),
        location=" / ".join(_locations(job, index)),
        url=text_field(job, "absolute_url", index) or "",
        description=_description(text_field(job, "content", index) or ""),
    )


def _locations(job: dict, index: int) -> list[str]:
    """The job's location (optional), then its offices', each once."""
    places = []
    location, offices = job.get("location"), job.get("offices")
    offices = [] if offices is None else offices
    if not isinstance(offices, list):
        raise BoardError(f"job {index}: `offices` is not a list")
    for record in ([] if location is None else [location]) + offices:
        if not isinstance(record, dict):
            raise BoardError(f"job {index}: a location or office is not an object")
        name = (text_field(record, "name", index) or "").strip()
        if name and name not in places:
            places.append(name)
    return places


def _description(content: str) -> str:
    """`content` is HTML with its markup entity-escaped, once in live payloads (`&lt;p&gt;`) and twice in
    Greenhouse's documentation (`&amp;lt;p&amp;gt;`): unescape until the tags are real, then strip them."""
    for _ in range(2):
        if REAL_TAG.search(content) or not ESCAPED_MARKUP.search(content):
            break  # real markup: what's still escaped is text, such as a literal "<customer>"
        content = html.unescape(content)
    return html_to_text(content)
