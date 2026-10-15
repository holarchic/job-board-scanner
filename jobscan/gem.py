"""Gem adapter: board URLs → API URL, and the public job board API payload → postings.

The payload is a bare list of jobs, shaped like Greenhouse's. Parsing is all-or-nothing per board, as for the
others, except that a demo job is skipped (and closed, if it was open). Gem's trial and integration-sandbox boards
carry the same six demo jobs, CFO to intern, whose whole description is DEMO; sandbox boards are refused outright."""
import json
from urllib.parse import quote

from jobscan.payload import board_slug, html_title, html_to_text, http_url, named_company, text_field, with_remote
from jobscan.records import Board, BoardError, Company, Get, Posting

ATS = "gem"
DISPLAY_NAME = "Gem"
describe = None  # the list carries each job's description
CRAWL_HOSTS = ("jobs.gem.com",)
API_PATH = ("job_board", "v0")
API = "https://api.gem.com/" + "/".join(API_PATH) + "/"
LIST = "/job_posts/"
BOARD_PAGE = "https://jobs.gem.com/{slug}"
DEMO = "***DEMO JOB POST***"


# Gem board paths are case-sensitive (`BigPanda` is a 404 where `bigpanda` reads), so a slug keeps its case.
# `fetch.board_for` tries every adapter's api_board before any page_board.


def api_board(company: str, url: str) -> Board | None:
    """A job board API URL on any host (how tests point at a fake server); the host is kept."""
    parsed = http_url(url)
    path = [p for p in parsed.path.split("/") if p] if parsed else []
    if tuple(path[:2]) != API_PATH or len(path) < 3:
        return None
    return _board(company, board_slug(path[2]), f"{parsed.scheme}://{parsed.netloc}/{'/'.join(API_PATH)}/")


def page_board(company: str, url: str) -> Board | None:
    """A board page, jobs.gem.com/<slug>[/…]."""
    parsed = http_url(url)
    path = [p for p in parsed.path.split("/") if p] if parsed else []
    if not path or parsed.hostname != CRAWL_HOSTS[0]:
        return None
    return _board(company, board_slug(path[0]), API)


def _board(company: str, slug: str | None, base: str) -> Board | None:
    if slug is None or _sandbox(slug):
        return None
    return Board(ats=ATS, slug=slug, company=company or slug, api_url=base + quote(slug, safe="") + LIST)


def _sandbox(slug: str) -> bool:
    """An integration sandbox's board (`vercel-ats-sandbox`, `sandboxschonfeld-com`): test postings only."""
    return "sandbox" in slug.casefold()


def company(board: Board, get: Get) -> Company:
    """The board page's title, `<name> Careers`; the job board API names no company."""
    title = html_title(get(BOARD_PAGE.format(slug=quote(board.slug, safe=""))))
    return named_company(title.strip().removesuffix(" Careers"))


def read(board: Board, get: Get) -> tuple[Posting, ...]:
    """A sandbox board still in the discovered list (stored before sandboxes were refused) reads as empty, without
    a request, so its postings close."""
    if _sandbox(board.slug):
        return ()
    return parse(board, json.loads(get(board.api_url)))


def parse(board: Board, data) -> tuple[Posting, ...]:
    if not isinstance(data, list):
        raise BoardError("response is not a list of jobs")
    postings = tuple(_posting(board, i, job) for i, job in enumerate(data))
    if len({p.key for p in postings}) != len(postings):
        raise BoardError("duplicate job ids")
    return tuple(p for p in postings if p.description != DEMO)


def _posting(board: Board, index: int, job) -> Posting:
    if not isinstance(job, dict):
        raise BoardError(f"job {index} is not an object")
    plain = text_field(job, "content_plain", index)
    rich = text_field(job, "content", index)
    return Posting(
        key=f"{board.id}:{text_field(job, 'id', index, required=True)}",
        ats=ATS,
        board=board.id,
        company=board.company,
        title=text_field(job, "title", index, required=True).strip(),
        location=" / ".join(_locations(job, index)),
        url=text_field(job, "absolute_url", index) or "",
        description=plain.strip() if plain and plain.strip() else html_to_text(rich or ""),
    )


def _locations(job: dict, index: int) -> list[str]:
    """The job's location, then each office's name and location, each once; `Remote` when the job is remote and
    no name says so."""
    offices = job.get("offices")
    offices = [] if offices is None else offices
    if not isinstance(offices, list) or not all(isinstance(o, dict) for o in offices):
        raise BoardError(f"job {index}: `offices` is not a list of objects")
    names = [_place_name(job, index)]
    for office in offices:
        names += [(text_field(office, "name", index) or "").strip(), _place_name(office, index)]
    places = []
    for name in names:
        if name and name not in places:
            places.append(name)
    return with_remote(places) if text_field(job, "location_type", index) == "remote" else places


def _place_name(record: dict, index: int) -> str:
    place = record.get("location")
    if place is None:
        return ""
    if not isinstance(place, dict):
        raise BoardError(f"job {index}: a `location` is not an object")
    return (text_field(place, "name", index) or "").strip()
