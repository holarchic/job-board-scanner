"""Teamtailor adapter: board URLs → feed URL, and a board's RSS feed → postings.

A board is a subdomain, <slug>.teamtailor.com, listing its jobs as RSS 2.0 with Teamtailor's location elements.
The feed stops at 100 items unless asked for a page size, so it is read with `per_page`; a feed that fills the page
may have been cut, and fails the board rather than close the postings past the cut. Parsing is all-or-nothing per
board, as for the others."""
from urllib.parse import quote
from xml.etree.ElementTree import Element

from jobscan.payload import board_slug, html_to_text, http_url, with_remote, xml_root
from jobscan.records import Board, BoardError, Posting

ATS = "teamtailor"
DISPLAY_NAME = "Teamtailor"
DECODE = xml_root
DOMAIN = "teamtailor.com"
CRAWL_HOSTS = (f"*.{DOMAIN}",)
FEED = "https://{slug}." + DOMAIN + "/jobs.rss"
PER_PAGE = 1000  # the most jobs seen on one board is 313
NOT_BOARDS = frozenset({"www", "app"})  # Teamtailor's own subdomains; `app` answers 200 with HTML, never a 404
TT = "{https://teamtailor.com/locations}"  # namespace of the location elements


def api_board(company: str, url: str) -> Board | None:
    """None: the feed lives on the board's own host, which page_board reads, and `/jobs.rss` on any other host
    names no Teamtailor board. (`fetch.board_for` tries every api_board first.)"""
    return None


def page_board(company: str, url: str) -> Board | None:
    """Any URL on <slug>.teamtailor.com. Host names are case-insensitive, so the slug is lower case."""
    parsed = http_url(url)
    labels = parsed.hostname.split(".") if parsed else []
    if len(labels) != 3 or ".".join(labels[1:]) != DOMAIN:
        return None
    slug = board_slug(labels[0], decoded=True)
    if slug is None or slug in NOT_BOARDS:
        return None
    return Board(ats=ATS, slug=slug, company=company or slug,
                 api_url=FEED.format(slug=quote(slug, safe="")) + f"?per_page={PER_PAGE}")


def parse(board: Board, root: Element) -> tuple[Posting, ...]:
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        raise BoardError("feed is not RSS")
    items = channel.findall("item")
    if len(items) >= PER_PAGE:
        raise BoardError(f"feed holds {len(items)} jobs, a full page: it may be cut")
    postings = tuple(_posting(board, index, item) for index, item in enumerate(items))
    if len({p.key for p in postings}) != len(postings):
        raise BoardError("duplicate job guids")
    return postings


def _posting(board: Board, index: int, item: Element) -> Posting:
    guid, title = _text(item, "guid"), _text(item, "title")
    if not guid or not title:
        raise BoardError(f"job {index}: no guid or title")
    return Posting(key=f"{board.id}:{guid}", ats=ATS, board=board.id, company=board.company, title=title,
                   location=" / ".join(_places(item)), url=_text(item, "link"),
                   description=html_to_text(_text(item, "description")))


def _places(item: Element) -> list[str]:
    """`city, country` per location (its name when it has neither), each once; `Remote` for a fully remote job."""
    places = []
    for location in item.iter(TT + "location"):
        parts = dict.fromkeys(p for p in (_text(location, TT + "city"), _text(location, TT + "country")) if p)
        place = ", ".join(parts) or _text(location, TT + "name")
        if place and place not in places:
            places.append(place)
    return with_remote(places) if _text(item, "remoteStatus") == "fully" else places


def _text(element: Element, path: str) -> str:
    return (element.findtext(path) or "").strip()
