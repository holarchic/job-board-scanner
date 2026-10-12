"""Ashby adapter: board URLs → API URL, and the public posting-API payload → postings.

Parsing is all-or-nothing per board: one malformed listed job fails the board, so a partial snapshot
never closes postings that are still live."""
from urllib.parse import urlparse

from jobscan.payload import html_to_text, text_field
from jobscan.records import Board, BoardError, Posting

ATS = "ashby"
DISPLAY_NAME = "Ashby"
CRAWL_HOSTS = ("jobs.ashbyhq.com",)
API = "https://api.ashbyhq.com/posting-api/job-board/"


def board(company: str, board_url: str) -> Board | None:
    """A Board for either URL form the map stores: the human board URL (jobs.ashbyhq.com/<slug>) or a posting-API
    URL on any host (how tests point at a fake server), normalised to scheme://host/posting-api/job-board/<slug>.
    Ashby slugs are case-insensitive, so the slug is case-folded: one board, one id and one set of posting keys,
    however a source writes it. None when the URL isn't recognisably Ashby's."""
    try:
        parsed = urlparse(board_url)
        parsed.port  # noqa: B018 -- raises on a malformed port
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    path = [p for p in parsed.path.split("/") if p]
    if path[:2] == ["posting-api", "job-board"] and len(path) > 2:
        slug = path[2].lower()
        api_url = f"{parsed.scheme}://{parsed.netloc}/posting-api/job-board/{slug}"
    elif parsed.netloc == "jobs.ashbyhq.com" and path:
        slug = path[0].lower()
        api_url = API + slug
    else:
        return None
    return Board(ats=ATS, slug=slug, company=company or slug, api_url=api_url)


def parse(board: Board, data) -> tuple[Posting, ...]:
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise BoardError("response has no `jobs` list")
    postings = tuple(p for p in (_posting(board, i, job) for i, job in enumerate(jobs)) if p is not None)
    if len({p.key for p in postings}) != len(postings):
        raise BoardError("duplicate listed job ids")
    return postings


def _posting(board: Board, index: int, job) -> Posting | None:
    if not isinstance(job, dict):
        raise BoardError(f"job {index} is not an object")
    listed = job.get("isListed", True)
    if not isinstance(listed, bool):
        raise BoardError(f"job {index}: `isListed` is not a boolean")
    if not listed:
        return None
    job_id = text_field(job, "id", index, required=True)
    title = text_field(job, "title", index, required=True)
    secondary = job.get("secondaryLocations")
    secondary = [] if secondary is None else secondary
    if not isinstance(secondary, list) or not all(isinstance(s, dict) for s in secondary):
        raise BoardError(f"job {index}: `secondaryLocations` is not a list of objects")
    locations = [text_field(job, "location", index)] + [text_field(s, "location", index) for s in secondary]
    return Posting(
        key=f"{board.id}:{job_id}",
        ats=ATS,
        board=board.id,
        company=board.company,
        title=title.strip(),
        location=" / ".join(loc for loc in locations if loc),
        url=text_field(job, "jobUrl", index) or "",
        description=_description(text_field(job, "descriptionPlain", index), text_field(job, "descriptionHtml", index)),
    )


def _description(plain: str | None, rich: str | None) -> str:
    if plain and plain.strip():
        return plain.strip()
    return html_to_text(rich or "")
