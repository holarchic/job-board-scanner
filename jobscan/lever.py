"""Lever adapter: board URLs → API URL, and the public postings API payload → postings.

Lever runs two instances: a board on jobs.eu.lever.co answers only on api.eu.lever.co (a 404 on the US host), so the
API host follows from the board URL's host. The list carries every job with its description, unpaged. Parsing is
all-or-nothing per board, as for the others.

Boards come from the boards file only: Common Crawl holds no readable captures of jobs.lever.co, so discovery can't
find them."""
import json
from urllib.parse import quote

from jobscan.payload import (
    board_slug,
    encodable,
    html_title,
    html_to_text,
    http_url,
    named_company,
    text_field,
    with_remote,
)
from jobscan.records import Board, BoardError, Company, Get, Places, Posting

ATS = "lever"
DISPLAY_NAME = "Lever"
same_job = None  # each posting is its own job
describe = None  # the list carries each job's description
CRAWL_HOSTS = ()  # not discovered: see the module docstring
INSTANCES = {"jobs.lever.co": "api.lever.co", "jobs.eu.lever.co": "api.eu.lever.co"}  # board host → API host
API_PATH = ("v0", "postings")
API = "https://{host}/" + "/".join(API_PATH) + "/"  # host templates, composed at call time
BOARD_PAGE = "https://{host}/{slug}"
LIST = "?mode=json"


# Lever slugs are case-sensitive (`Spotify` is a 404 where `spotify` reads), so a slug keeps its case.
# `fetch.board_for` tries every adapter's api_board before any page_board.


def api_board(company: str, url: str) -> Board | None:
    """A postings API URL on any host (how tests point at a fake server); the host is kept."""
    parsed = http_url(url)
    path = [p for p in parsed.path.split("/") if p] if parsed else []
    if tuple(path[:2]) != API_PATH or len(path) < 3:
        return None
    return _board(company, board_slug(path[2]), f"{parsed.scheme}://{parsed.netloc}/{'/'.join(API_PATH)}/")


def page_board(company: str, url: str) -> Board | None:
    """A board page on either instance, jobs.lever.co/<slug>[/…] or jobs.eu.lever.co/<slug>[/…]."""
    parsed = http_url(url)
    path = [p for p in parsed.path.split("/") if p] if parsed else []
    if not path or parsed.hostname not in INSTANCES:
        return None
    return _board(company, board_slug(path[0]), API.format(host=INSTANCES[parsed.hostname]))


def _board(company: str, slug: str | None, base: str) -> Board | None:
    if slug is None:
        return None
    return Board(ats=ATS, slug=slug, company=company or slug, api_url=base + quote(slug, safe="") + LIST)


def company(board: Board, get: Get) -> Company:
    """The board page's title, the company's name; the postings API names no company. A board read through an API
    URL on another host has no page to ask."""
    for page_host, api_host in INSTANCES.items():
        if board.api_url.startswith(API.format(host=api_host)):
            return named_company(html_title(get(BOARD_PAGE.format(host=page_host, slug=quote(board.slug, safe="")))))
    raise BoardError("not a Lever instance's API: no board page to read")


def read(board: Board, get: Get, places: Places) -> tuple[Posting, ...]:
    return parse(board, json.loads(get(board.api_url)))


def parse(board: Board, data) -> tuple[Posting, ...]:
    if not isinstance(data, list):
        raise BoardError("response is not a list of jobs")
    postings = tuple(_posting(board, i, job) for i, job in enumerate(data))
    if len({p.key for p in postings}) != len(postings):
        raise BoardError("duplicate job ids")
    return postings


def _posting(board: Board, index: int, job) -> Posting:
    if not isinstance(job, dict):
        raise BoardError(f"job {index} is not an object")
    places = _places(job, index)
    return Posting(
        key=f"{board.id}:{text_field(job, 'id', index, required=True)}",
        ats=ATS,
        board=board.id,
        company=board.company,
        title=text_field(job, "text", index, required=True).strip(),
        location=" / ".join(with_remote(places) if text_field(job, "workplaceType", index) == "remote" else places),
        url=text_field(job, "hostedUrl", index) or "",
        description=_description(job, index),
    )


def _places(job: dict, index: int) -> list[str]:
    """`allLocations`, each once, else `location`."""
    categories = job.get("categories")
    categories = {} if categories is None else categories
    if not isinstance(categories, dict):
        raise BoardError(f"job {index}: `categories` is not an object")
    every = categories.get("allLocations")
    if every is None:
        every = [text_field(categories, "location", index) or ""]
    elif not isinstance(every, list) or not all(isinstance(p, str) and encodable(p) for p in every):
        raise BoardError(f"job {index}: `allLocations` is not a list of strings")
    return [p for p in dict.fromkeys(p.strip() for p in every) if p]


def _description(job: dict, index: int) -> str:
    """The description (Lever's opening and body), each list under its heading, then the closing text."""
    lists = job.get("lists")
    lists = [] if lists is None else lists
    if not isinstance(lists, list) or not all(isinstance(e, dict) for e in lists):
        raise BoardError(f"job {index}: `lists` is not a list of objects")
    parts = [html_to_text(text_field(job, "description", index) or "")]
    for entry in lists:
        parts += [(text_field(entry, "text", index) or "").strip(),
                  html_to_text(text_field(entry, "content", index) or "")]
    parts.append(html_to_text(text_field(job, "additional", index) or ""))
    return "\n".join(p for p in parts if p)
