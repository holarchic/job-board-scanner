"""Board fetching: board list → postings per board through the ATS adapter, one board's failure isolated
from the rest. A board counts as read only on HTTP 200 with a payload its adapter parses completely."""
import http.client
import itertools
import json
import logging
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import ExitStack
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from jobscan import ashby, gem, greenhouse, oracle, personio, rippling, smartrecruiters, teamtailor, workable, workday
from jobscan.config import ConfigError
from jobscan.payload import encodable
from jobscan.records import Board, BoardError, Company, Places, Posting

ADAPTERS = {ashby.ATS: ashby, greenhouse.ATS: greenhouse, workable.ATS: workable, gem.ATS: gem,
            teamtailor.ATS: teamtailor, personio.ATS: personio, smartrecruiters.ATS: smartrecruiters,
            rippling.ATS: rippling, workday.ATS: workday, oracle.ATS: oracle}
TIMEOUT = 20
RETRY_PAUSES = (2, 10)  # seconds before the 2nd and 3rd tries
RETRYABLE = {429, 500, 502, 503, 504}
WORKERS = 8  # per ATS lane (`fetch_each`); describe_all and company_all run one pool of this size
IN_FLIGHT = 2 * WORKERS  # per lane: boards fetching or fetched but not yet consumed, bounding postings held
HEADERS = {"User-Agent": "jobscan (+https://github.com/holarchic/job-board-scanner)", "Accept-Encoding": "gzip"}
MAX_BODY = 256 << 20  # bytes an answer may inflate to; the largest board seen (Anduril's Greenhouse) inflates to 44 MB
# OSError covers URLError, timeouts and resets; ValueError covers bad JSON and bad UTF-8;
# RecursionError covers deeply nested JSON on Python < 3.14. XML decoding raises BoardError itself.
FAILURES = (OSError, http.client.HTTPException, ValueError, RecursionError, BoardError)
log = logging.getLogger("jobscan")


@dataclass(frozen=True)
class BoardFetch:
    board: Board
    postings: tuple[Posting, ...]


@dataclass(frozen=True)
class BoardFailure:
    board: Board
    reason: str
    http_status: int | None = None  # 404 means the board is gone; anything else may clear by the next run


@dataclass(frozen=True)
class Described:
    posting: Posting  # with the description its detail request returned


@dataclass(frozen=True)
class DescribeFailure:
    posting: Posting
    reason: str
    http_status: int | None = None  # 404: the posting is gone from its board


@dataclass(frozen=True)
class CompanyFound:
    board: Board
    company: Company


@dataclass(frozen=True)
class CompanyFailure:
    board: Board
    reason: str
    http_status: int | None = None


def load_boards(path: Path) -> tuple[list[Board], int]:
    """Boards some adapter recognises, from a {"companies": [{"company", "board_url"}]} file, deduplicated by
    board id, plus the count of entries no adapter recognises."""
    boards, skipped = {}, 0
    for entry in read_boards_file(path)["companies"]:
        found = entry_board(entry)
        if found is None:
            skipped += 1
            continue
        boards.setdefault(found.id, found)
    return list(boards.values()), skipped


def read_boards_file(path: Path) -> dict:
    """The boards file's object, its `companies` a list of objects whose text is storable."""
    try:
        data = json.loads(path.read_text())
        entries = data["companies"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as e:
        raise ConfigError(f"{path}: not a readable boards file ({type(e).__name__}: {e})") from None
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise ConfigError(f"{path}: `companies` must be a list of objects")
    for n, entry in enumerate(entries):
        for text in (entry.get("company"), entry.get("board_url")):
            if isinstance(text, str) and not encodable(text):  # a lone surrogate can't be stored
                raise ConfigError(f"{path}: entry {n} is not valid Unicode")
    return data


def entry_board(entry: dict) -> Board | None:
    """The board a boards-file entry names, or None when no adapter knows its URL."""
    company, url = entry.get("company"), entry.get("board_url")
    return board_for(company if isinstance(company, str) else "", url if isinstance(url, str) else "")


def board_for(company: str, url: str) -> Board | None:
    """The board a URL names, or None. API URLs come first, on any host (tests point at fakes), so a path one ATS
    uses for its API is never read as another ATS's board page."""
    for adapter in ADAPTERS.values():
        if (found := adapter.api_board(company, url)) is not None:
            return found
    for adapter in ADAPTERS.values():
        if (found := adapter.page_board(company, url)) is not None:
            return found
    return None



def same_job(key: str) -> str:
    """The key every posting of one job shares, by its ATS (Workday: one requisition on a tenant's several sites); the
    posting's own key when its ATS lists each job once."""
    adapter = ADAPTERS[key.split(":", 1)[0]]
    return key if adapter.same_job is None else adapter.same_job(key)


def fetch_each(boards: list[Board], places: Places) -> Iterator[BoardFetch | BoardFailure]:
    """Every board's outcome as it completes; `places` is the profile's location test, which an adapter may read only
    the matching postings by (Workday does). Each ATS reads in its own lane of WORKERS, every lane at once: no ATS
    sees more requests at a time than one shared pool gave it, and the fetch lasts as long as its slowest ATS rather
    than all of them end to end. A board is submitted only as an earlier outcome of its lane is handed over, so at
    most IN_FLIGHT boards' postings per lane are held here (plus whatever the caller keeps), however many boards."""
    lanes: dict[str, list[Board]] = {}
    for board in boards:
        lanes.setdefault(board.ats, []).append(board)
    queues = {ats: iter(lane) for ats, lane in lanes.items()}
    left = {ats: len(lane) for ats, lane in lanes.items()}
    started = time.monotonic()
    with ExitStack() as stack:
        pools = {ats: stack.enter_context(ThreadPoolExecutor(WORKERS)) for ats in lanes}
        running: dict[Future, str] = {}

        def submit(ats: str, n: int) -> None:
            for board in itertools.islice(queues[ats], n):
                running[pools[ats].submit(_fetch_isolated, board, places)] = ats

        for ats in lanes:
            submit(ats, IN_FLIGHT)
        while running:
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            while done:
                future = done.pop()  # popped and dropped, so the finished future and its postings aren't kept
                ats, outcome = running.pop(future), future.result()
                del future
                submit(ats, 1)
                left[ats] -= 1
                if not left[ats]:
                    log.info("fetch_lane_done ats=%s boards=%d seconds=%.0f", ats, len(lanes[ats]),
                             time.monotonic() - started)
                yield outcome
                del outcome


def _fetch_isolated(board: Board, places: Places) -> BoardFetch | BoardFailure:
    adapter = ADAPTERS[board.ats]
    try:
        return BoardFetch(board, adapter.read(board, partial(_get, board), places))
    except FAILURES as e:
        return BoardFailure(board, _reason(e), _status(e))


def describe_all(pairs: list[tuple[Board, Posting]]) -> list[Described | DescribeFailure]:
    """Each posting with the description its adapter's detail request returns, in order, WORKERS at a time. Only
    for adapters whose `describe` isn't None; one posting's failure never stops the rest."""
    with ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(_describe_isolated, pairs))


def _describe_isolated(pair: tuple[Board, Posting]) -> Described | DescribeFailure:
    board, posting = pair
    try:
        description = ADAPTERS[board.ats].describe(board, posting, partial(_get, board))
        return Described(replace(posting, description=description))
    except FAILURES as e:
        return DescribeFailure(posting, _reason(e), _status(e))


def company_all(boards: list[Board]) -> list[CompanyFound | CompanyFailure]:
    """Each board's company as its adapter looks it up, in order, WORKERS at a time; one board's failure never stops
    the rest."""
    with ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(_company_isolated, boards))


def _company_isolated(board: Board) -> CompanyFound | CompanyFailure:
    try:
        return CompanyFound(board, ADAPTERS[board.ats].company(board, partial(_get, board)))
    except FAILURES as e:
        return CompanyFailure(board, _reason(e), _status(e))


def _get(board: Board, url: str, body: bytes | None = None) -> bytes:
    """One request on the board's behalf, a GET, or a POST of `body` as JSON: its 429s are logged under its id."""
    headers = HEADERS if body is None else HEADERS | {"Content-Type": "application/json"}
    request = urllib.request.Request(url, data=body, headers=headers)
    for attempt, pause in enumerate((*RETRY_PAUSES, None), start=1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                if response.status != 200:
                    raise BoardError(f"HTTP {response.status}")
                # Read whole, without a size: a connection cut short then raises IncompleteRead, which is retried.
                return _decoded(response.read(), response.headers.get("Content-Encoding"))
        except urllib.error.HTTPError as e:
            e.close()
            if e.code == 429:  # every one, so rate limiting shows even when a retry then succeeds
                log.warning("board_429 board=%s attempt=%d", board.id, attempt)
            if e.code not in RETRYABLE or pause is None:
                raise
        except (OSError, http.client.HTTPException):
            if pause is None:
                raise
        time.sleep(pause)


def _decoded(body: bytes, encoding: str | None) -> bytes:
    """The answer's bytes as sent before its Content-Encoding: gzip (the one asked for) inflated to at most
    MAX_BODY; any other encoding fails the board."""
    if encoding is None or encoding.strip().casefold() == "identity":
        return body
    if encoding.strip().casefold() != "gzip":
        raise BoardError(f"answer encoded {encoding!r}, not asked for")
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)  # gzip framing
    try:
        inflated = inflater.decompress(body, MAX_BODY + 1)
    except zlib.error:
        raise BoardError("malformed gzip") from None
    if len(inflated) > MAX_BODY:
        raise BoardError(f"answer inflates past {MAX_BODY >> 20} MB")
    if not inflater.eof:
        raise BoardError("malformed gzip: the stream ends early")
    return inflated


def _status(e: Exception) -> int | None:
    return e.code if isinstance(e, urllib.error.HTTPError) else None


def _reason(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"network: {e.reason}"
    if isinstance(e, (OSError, http.client.HTTPException)):
        return f"network: {type(e).__name__}"
    if isinstance(e, (ValueError, RecursionError)):
        return "malformed JSON"
    return str(e) or type(e).__name__
