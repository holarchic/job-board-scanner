"""The `discover` pipeline: the newest Common Crawl crawls → every board an ATS adapter recognises in them → the
install's discovered board list. The list is replaced only when every crawl read completely."""
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jobscan import crawl, fetch, store
from jobscan.config import ConfigError
from jobscan.records import Board

CRAWLS = 3  # Ashby: one crawl finds 91% of a hand-built board list, three 97%, a fourth nothing more
STALE_AFTER = timedelta(days=30)  # Common Crawl publishes about one crawl a month
log = logging.getLogger("jobscan")


@dataclass(frozen=True)
class DiscoverOutcome:
    boards: int
    crawls: tuple[str, ...]


def refresh_if_stale(conn: sqlite3.Connection, now: datetime) -> None:
    """`run`'s monthly discovery, so one scheduled job is enough; also as soon as an ATS gains an adapter with crawl
    hosts, so an upgraded install reads it on its next run. An unreadable crawl keeps the old list and the run goes
    on; a list that can't be saved is a ConfigError, as for `discover`."""
    last = store.discovered_at(conn)
    discoverable = {a.ATS for a in fetch.ADAPTERS.values() if a.CRAWL_HOSTS}
    if last is not None and now - last < STALE_AFTER and store.discovered_ats(conn) >= discoverable:
        return
    try:
        discover(conn)
    except crawl.CrawlError as e:
        log.warning("discover_failed error=%r kept=previous_list", str(e))


def discover(conn: sqlite3.Connection) -> DiscoverOutcome:
    """Raises crawl.CrawlError when any crawl can't be read, and ConfigError when the list can't be saved; either
    way the previous list stays in place. The caller holds the run lock."""
    base = crawl.BASE
    crawls = crawl.latest_crawls(CRAWLS, base)
    boards: dict[str, Board] = {}
    for adapter in fetch.ADAPTERS.values():
        for host in adapter.CRAWL_HOSTS:
            for crawl_id in crawls:
                urls = crawl.captures(crawl_id, host, base)
                boards_seen = (fetch.board_for("", url) for url in urls)
                found = [b for b in boards_seen if b is not None and b.ats == adapter.ATS]
                log.info("discover crawl=%s host=%s captures=%d boards=%d", crawl_id, host, len(urls),
                         len({b.id for b in found}))
                for board in found:
                    boards.setdefault(board.id, board)
    try:
        store.replace_discovered(conn, list(boards.values()), datetime.now(UTC).isoformat(timespec="microseconds"))
    except sqlite3.Error as e:
        raise ConfigError(f"cannot save the board list ({type(e).__name__}: {e})") from None
    log.info("discover_done boards=%d crawls=%s", len(boards), ",".join(crawls))
    return DiscoverOutcome(boards=len(boards), crawls=tuple(crawls))
