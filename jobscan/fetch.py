"""Board fetching: board list → postings per board through the ATS adapter, one board's failure isolated
from the rest. A board counts as read only on HTTP 200 with a payload its adapter parses completely."""
import http.client
import itertools
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from jobscan import ashby, gem, greenhouse, personio, teamtailor, workable
from jobscan.config import ConfigError
from jobscan.records import Board, BoardError, Posting

ADAPTERS = {ashby.ATS: ashby, greenhouse.ATS: greenhouse, workable.ATS: workable, gem.ATS: gem,
            teamtailor.ATS: teamtailor, personio.ATS: personio}
TIMEOUT = 20
RETRY_PAUSES = (2, 10)  # seconds before the 2nd and 3rd tries
RETRYABLE = {429, 500, 502, 503, 504}
WORKERS = 8
IN_FLIGHT = 2 * WORKERS  # boards fetching or fetched but not yet consumed: bounds postings held in memory
HEADERS = {"User-Agent": "jobscan (+https://github.com/holarchic/job-board-scanner)"}
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


def load_boards(path: Path) -> tuple[list[Board], int]:
    """Boards some adapter recognises, from a {"companies": [{"company", "board_url"}]} file, deduplicated by
    board id, plus the count of entries no adapter recognises."""
    try:
        entries = json.loads(path.read_text())["companies"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as e:
        raise ConfigError(f"{path}: not a readable boards file ({type(e).__name__}: {e})") from None
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise ConfigError(f"{path}: `companies` must be a list of objects")
    boards, skipped = {}, 0
    for n, entry in enumerate(entries):
        company, url = entry.get("company"), entry.get("board_url")
        for text in (company, url):
            if isinstance(text, str) and not _encodable(text):  # a lone surrogate can't be stored
                raise ConfigError(f"{path}: entry {n} is not valid Unicode")
        found = board_for(company if isinstance(company, str) else "", url if isinstance(url, str) else "")
        if found is None:
            skipped += 1
            continue
        boards.setdefault(found.id, found)
    return list(boards.values()), skipped


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


def _encodable(text: str) -> bool:
    try:
        text.encode()
    except UnicodeEncodeError:
        return False
    return True


def fetch_each(boards: list[Board]) -> Iterator[BoardFetch | BoardFailure]:
    """Every board's outcome as it completes. A board is submitted only as an earlier outcome is handed over, so
    at most IN_FLIGHT boards' postings are held here (plus whatever the caller keeps), however many boards there are."""
    queue = iter(boards)
    with ThreadPoolExecutor(WORKERS) as pool:
        running = {pool.submit(_fetch_isolated, b) for b in itertools.islice(queue, IN_FLIGHT)}
        while running:
            done, running = wait(running, return_when=FIRST_COMPLETED)
            while done:
                outcome = done.pop().result()  # popped, so the finished future and its postings aren't kept
                running |= {pool.submit(_fetch_isolated, b) for b in itertools.islice(queue, 1)}
                yield outcome
                del outcome


def _fetch_isolated(board: Board) -> BoardFetch | BoardFailure:
    adapter = ADAPTERS[board.ats]
    try:
        return BoardFetch(board, adapter.parse(board, adapter.DECODE(_get(board))))
    # OSError covers URLError, timeouts and resets; ValueError covers bad JSON and bad UTF-8;
    # RecursionError covers deeply nested JSON on Python < 3.14. XML decoding raises BoardError itself.
    except (OSError, http.client.HTTPException, ValueError, RecursionError, BoardError) as e:
        return BoardFailure(board, _reason(e), e.code if isinstance(e, urllib.error.HTTPError) else None)


def _get(board: Board) -> bytes:
    request = urllib.request.Request(board.api_url, headers=HEADERS)
    for attempt, pause in enumerate((*RETRY_PAUSES, None), start=1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                if response.status != 200:
                    raise BoardError(f"HTTP {response.status}")
                return response.read()
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
