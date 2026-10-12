"""Greenhouse adapter: board URLs → API URL, and the public Job Board API payload → postings.

One API host serves US and EU boards. Parsing is all-or-nothing per board, as for Ashby."""
import html
import re
from urllib.parse import parse_qs, quote, unquote, urlsplit

from jobscan.payload import html_to_text, text_field
from jobscan.records import Board, BoardError, Posting

ATS = "greenhouse"
DISPLAY_NAME = "Greenhouse"
# boards.greenhouse.io redirects to these, so Common Crawl holds no status-200 pages of its own.
CRAWL_HOSTS = ("job-boards.greenhouse.io", "job-boards.eu.greenhouse.io")
BOARD_HOSTS = {*CRAWL_HOSTS, "boards.greenhouse.io", "boards.eu.greenhouse.io"}
API = "https://boards-api.greenhouse.io/v1/boards/"
LIST = "/jobs?content=true"
# Board tokens include `ep+co` and `harrison&star`; ':' is the posting-key separator.
SLUG = re.compile(r"[!-~]+")
NOT_IN_SLUG = set("/:?#")
REAL_TAG = re.compile(r"</?[a-zA-Z]")
ESCAPED_TAG = re.compile(r"&(?:amp;)?lt;/?[a-zA-Z]")


def board(company: str, board_url: str) -> Board | None:
    """A Board for a Job Board API URL on any host (how tests point at a fake server), or a board page on a
    Greenhouse host, including the legacy embed form `…/embed/job_board?for=<slug>`. The slug is percent-decoded
    and case-folded (Greenhouse tokens are case-insensitive), so every spelling of a board is one board. None when
    the URL isn't recognisably Greenhouse's."""
    try:
        parsed = urlsplit(board_url)  # not urlparse: it would split `;params` off a token like `x;y`
        parsed.port  # noqa: B018 -- raises on a malformed port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    path = [p for p in parsed.path.split("/") if p]
    if path[:2] == ["v1", "boards"] and len(path) > 2:
        slug = _slug(path[2])
        base = f"{parsed.scheme}://{parsed.netloc}/v1/boards/"
    elif parsed.netloc in BOARD_HOSTS and path[:2] == ["embed", "job_board"]:
        slug = _slug(parse_qs(parsed.query).get("for", [""])[0], decoded=True)
        base = API
    elif parsed.netloc in BOARD_HOSTS and path and path[0] != "embed" and path[:2] != ["posting-api", "job-board"]:
        slug = _slug(path[0])
        base = API
    else:
        return None
    if slug is None:
        return None
    return Board(ats=ATS, slug=slug, company=company or slug, api_url=base + quote(slug, safe="") + LIST)


def _slug(raw: str, decoded: bool = False) -> str | None:
    slug = (raw if decoded else unquote(raw)).lower()
    if not SLUG.fullmatch(slug) or NOT_IN_SLUG & set(slug):
        return None
    return slug


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
    location = job.get("location")
    for record in ([] if location is None else [location]) + _list(job, "offices", index):
        if not isinstance(record, dict):
            raise BoardError(f"job {index}: a location or office is not an object")
        name = (text_field(record, "name", index) or "").strip()
        if name and name not in places:
            places.append(name)
    return places


def _list(job: dict, name: str, index: int) -> list:
    value = job.get(name)
    if value is None:
        return []
    if not isinstance(value, list):
        raise BoardError(f"job {index}: `{name}` is not a list")
    return value


def _description(content: str) -> str:
    """`content` is HTML with its markup entity-escaped, once in live payloads (`&lt;p&gt;`) and twice in
    Greenhouse's documentation (`&amp;lt;p&amp;gt;`): unescape until the tags are real, then strip them."""
    for _ in range(2):
        if REAL_TAG.search(content) or not ESCAPED_TAG.search(content):
            break  # real markup: what's still escaped is text, such as a literal "<customer>"
        content = html.unescape(content)
    return html_to_text(content)
