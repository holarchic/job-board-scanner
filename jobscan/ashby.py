"""Ashby adapter: board URLs → API URL, and the public posting-API payload → postings.

Parsing is all-or-nothing per board: one malformed listed job fails the board, so a partial snapshot
never closes postings that are still live."""
from jobscan.payload import html_to_text, http_url, text_field
from jobscan.records import Board, BoardError, Posting

ATS = "ashby"
DISPLAY_NAME = "Ashby"
CRAWL_HOSTS = ("jobs.ashbyhq.com",)
API = "https://api.ashbyhq.com/posting-api/job-board/"


# Ashby slugs are case-insensitive, so slugs are case-folded: one board, one id and one set of posting keys, however
# a source writes it. `fetch.board_for` tries every adapter's api_board before any page_board.


def api_board(company: str, url: str) -> Board | None:
    """A posting-API URL on any host (how tests point at a fake server), normalised to
    scheme://host/posting-api/job-board/<slug>."""
    parsed = http_url(url)
    path = [p for p in parsed.path.split("/") if p] if parsed else []
    if path[:2] != ["posting-api", "job-board"] or len(path) < 3:
        return None
    slug = path[2].lower()
    return Board(ats=ATS, slug=slug, company=company or slug,
                 api_url=f"{parsed.scheme}://{parsed.netloc}/posting-api/job-board/{slug}")


def page_board(company: str, url: str) -> Board | None:
    """A board page, jobs.ashbyhq.com/<slug>[/…]."""
    parsed = http_url(url)
    path = [p for p in parsed.path.split("/") if p] if parsed else []
    if not path or parsed.hostname != CRAWL_HOSTS[0]:
        return None
    slug = path[0].lower()
    return Board(ats=ATS, slug=slug, company=company or slug, api_url=API + slug)


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
