"""The boards file as `jobscan boards` edits it: list its entries, add board URLs. Its format and reading rules are
`fetch`'s; this module only appends, replacing the file in one rename so a run reads the old list or the new."""
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from jobscan import fetch
from jobscan.config import BOARDS_FILE, Config, ConfigError
from jobscan.records import Board


@dataclass(frozen=True)
class Entry:
    company: str
    url: str
    board: Board | None  # None: no adapter knows the URL, so a run skips it


@dataclass(frozen=True)
class Added:
    added: tuple[Board, ...]
    listed_already: tuple[Board, ...]


def path(config: Config) -> Path:
    """The file a run reads boards from, and where `add` writes: config.toml's `boards_file`, else BOARDS_FILE."""
    return config.boards_file or config.home / BOARDS_FILE


def entries(file: Path) -> list[Entry]:
    if not file.exists():
        return []
    return [Entry(company=e.get("company") if isinstance(e.get("company"), str) else "",
                  url=e.get("board_url") if isinstance(e.get("board_url"), str) else "", board=fetch.entry_board(e))
            for e in fetch.read_boards_file(file)["companies"]]


def add(file: Path, urls: list[str], company: str | None) -> Added:
    """Appends each URL's board unless the file lists it already. Every URL is checked before anything is written:
    one no adapter knows adds nothing. `company` names the board (one URL only); without it, a run looks it up."""
    if company is not None and len(urls) != 1:
        raise ConfigError("--company names one board: give one URL with it")
    if company is not None and not (company.strip() and _encodable(company)):
        raise ConfigError("--company: give the company's name as text")
    boards = []
    for url in urls:
        board = fetch.board_for(company or "", url)
        if board is None:
            raise ConfigError(f"{url}: not a job board URL jobscan knows; nothing was added")
        boards.append(board)
    data = fetch.read_boards_file(file) if file.exists() else {"companies": []}
    listed = {board.id for e in data["companies"] if (board := fetch.entry_board(e)) is not None}
    added, already = [], []
    for url, board in zip(urls, boards, strict=True):
        if board.id in listed:
            already.append(board)
            continue
        listed.add(board.id)
        added.append(board)
        data["companies"].append({"company": company, "board_url": url} if company else {"board_url": url})
    if added:
        _replace(file, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return Added(added=tuple(added), listed_already=tuple(already))


def _replace(file: Path, text: str) -> None:
    """A text that can't be encoded (a lone surrogate the file escaped) is refused before anything is written; a
    temporary file that can't be removed after a failure is named."""
    if not _encodable(text):
        raise ConfigError(f"{file} holds text that can't be written back (a lone surrogate); fix it by hand")
    try:
        fd, temp = tempfile.mkstemp(dir=file.parent, prefix=".boards-", suffix=".tmp")
    except OSError as e:
        raise ConfigError(f"could not write {file} ({type(e).__name__})") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(temp, file)
    except OSError as e:
        try:
            Path(temp).unlink(missing_ok=True)
            left = ""
        except OSError:
            left = f"; delete {temp}"
        raise ConfigError(f"could not write {file} ({type(e).__name__}); it is unchanged{left}") from None


def _encodable(text: str) -> bool:
    try:
        text.encode()
    except UnicodeEncodeError:
        return False
    return True
