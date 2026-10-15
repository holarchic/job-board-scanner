"""All persistent state: one SQLite file under $JOBSCAN_HOME. Only the main thread writes."""
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jobscan.config import ConfigError
from jobscan.records import Board, Company, Posting
from jobscan.tag import TagDef, TagResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    key TEXT PRIMARY KEY, ats TEXT NOT NULL, board TEXT NOT NULL, company TEXT NOT NULL, title TEXT NOT NULL,
    location TEXT NOT NULL, url TEXT NOT NULL, description TEXT NOT NULL, input_hash TEXT NOT NULL,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, closed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS postings_board ON postings (board);
CREATE TABLE IF NOT EXISTS tag_results (
    posting_key TEXT NOT NULL, input_hash TEXT NOT NULL, def_hash TEXT NOT NULL, model_id TEXT NOT NULL,
    value TEXT NOT NULL, quote TEXT, verified INTEGER NOT NULL, warn INTEGER NOT NULL,
    PRIMARY KEY (posting_key, input_hash, def_hash, model_id)
);
CREATE TABLE IF NOT EXISTS deliveries (
    profile TEXT NOT NULL, posting_key TEXT NOT NULL, digest_path TEXT NOT NULL, PRIMARY KEY (profile, posting_key)
);
CREATE TABLE IF NOT EXISTS boards (
    id TEXT PRIMARY KEY, ats TEXT NOT NULL, slug TEXT NOT NULL, api_url TEXT NOT NULL,
    not_found INTEGER NOT NULL DEFAULT 0,  -- consecutive HTTP 404s; at DEAD_AFTER, run skips the board
    discovered TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS companies (  -- by board id, apart from `boards`, so a lookup survives rediscovery
    board TEXT PRIMARY KEY, name TEXT NOT NULL, website TEXT, found TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    started TEXT NOT NULL, finished TEXT NOT NULL, boards_read INTEGER NOT NULL, boards_failed INTEGER NOT NULL,
    new INTEGER NOT NULL, tagged INTEGER NOT NULL, carried_over INTEGER NOT NULL, cost REAL NOT NULL,
    cost_complete INTEGER NOT NULL  -- 0 when some model reply reported no cost: `cost` is then a lower bound
);
"""
SCHEMA_VERSION = 1
DEAD_AFTER = 3


@dataclass(frozen=True)
class StoredPosting:
    posting: Posting
    input_hash: str
    first_seen: str


@dataclass(frozen=True)
class RunRecord:
    started: str
    finished: str
    boards_read: int
    boards_failed: int
    new: int
    tagged: int
    carried_over: int
    cost: float
    cost_complete: bool


def open_db(home: Path) -> sqlite3.Connection:
    """Opens (creating if needed) the install's database; a file that can't be opened is a setup error."""
    path = home / "jobscan.sqlite"
    try:
        conn = sqlite3.connect(path)
    except sqlite3.Error as e:
        raise ConfigError(f"{path}: cannot open database ({type(e).__name__})") from None
    try:
        conn.row_factory = sqlite3.Row
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ConfigError(f"{path}: database schema {version} is newer than this jobscan's ({SCHEMA_VERSION}): "
                              "update jobscan")
        conn.executescript(SCHEMA)
        _migrate(conn, version)
    except ConfigError:
        conn.close()
        raise
    except sqlite3.Error as e:
        conn.close()
        raise ConfigError(f"{path}: cannot open database ({type(e).__name__}: {e})") from None
    return conn


def _migrate(conn: sqlite3.Connection, version: int) -> None:
    """Version 1: Ashby slugs are case-folded in board ids and posting keys (`ashby:Clerk:1` → `ashby:clerk:1`), so
    deliveries and cached tags follow their postings. One transaction; a key collision rolls it all back."""
    if version == SCHEMA_VERSION:
        return
    conn.create_function("fold_slug", 1, _fold_slug, deterministic=True)
    with conn:
        conn.execute("UPDATE postings SET key = fold_slug(key), board = fold_slug(board) WHERE ats = 'ashby'")
        conn.execute("UPDATE deliveries SET posting_key = fold_slug(posting_key) WHERE posting_key LIKE 'ashby:%'")
        conn.execute("UPDATE tag_results SET posting_key = fold_slug(posting_key) WHERE posting_key LIKE 'ashby:%'")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _fold_slug(value: str) -> str:
    """`ashby:Slug` → `ashby:slug`; `ashby:Slug:rest` → `ashby:slug:rest`."""
    parts = value.split(":", 2)
    parts[1] = parts[1].lower()
    return ":".join(parts)


def input_hash(posting: Posting) -> str:
    """Hash of everything the model sees, so an edit to title, location or description re-tags."""
    return hashlib.sha256("\x1f".join([posting.title, posting.location, posting.description]).encode()).hexdigest()


def apply_fetch(conn: sqlite3.Connection, board: str, postings: tuple[Posting, ...], now: str) -> None:
    """Record one successfully read board as the full truth for that board: what it lists is open (re-seen
    postings reopen), everything else of the board's is closed. Never call this for a board whose fetch failed."""
    with conn:
        conn.execute("UPDATE postings SET closed = 1 WHERE board = ?", (board,))
        conn.executemany(
            """INSERT INTO postings (key, ats, board, company, title, location, url, description, input_hash,
                                     first_seen, last_seen, closed)
               VALUES (:key, :ats, :board, :company, :title, :location, :url, :description, :input_hash,
                       :now, :now, 0)
               ON CONFLICT (key) DO UPDATE SET
                   company = excluded.company, title = excluded.title, location = excluded.location,
                   url = excluded.url, description = excluded.description, input_hash = excluded.input_hash,
                   last_seen = excluded.last_seen, closed = 0""",
            [{**vars(p), "input_hash": input_hash(p), "now": now} for p in postings],
        )


def pending(conn: sqlite3.Connection, profile: str, boards: set[str]) -> Iterator[StoredPosting]:
    """Open postings of these boards never delivered to this profile, oldest first, one at a time: the caller
    filters as it reads, so every stored description is never in memory at once."""
    rows = conn.execute(
        """SELECT * FROM postings
           WHERE closed = 0 AND key NOT IN (SELECT posting_key FROM deliveries WHERE profile = ?)
           ORDER BY first_seen, key""",
        (profile,),
    )
    for r in rows:
        if r["board"] not in boards:
            continue
        yield StoredPosting(
            posting=Posting(key=r["key"], ats=r["ats"], board=r["board"], company=r["company"], title=r["title"],
                            location=r["location"], url=r["url"], description=r["description"]),
            input_hash=r["input_hash"],
            first_seen=r["first_seen"],
        )


def open_counts(conn: sqlite3.Connection, boards: set[str]) -> dict[str, int]:
    """Open postings per board, every role, not only matches: a rough size signal. A board that failed this run
    keeps its last-known count."""
    counts = dict.fromkeys(boards, 0)
    for r in conn.execute("SELECT board, COUNT(*) AS n FROM postings WHERE closed = 0 GROUP BY board"):
        if r["board"] in counts:
            counts[r["board"]] = r["n"]
    return counts


def replace_discovered(conn: sqlite3.Connection, boards: list[Board], now: str) -> None:
    """Make `boards` the discovered set. A board still listed keeps its 404 count: being in an old crawl is no
    evidence it exists today."""
    with conn:
        counts = {r["id"]: r["not_found"] for r in conn.execute("SELECT id, not_found FROM boards")}
        conn.execute("DELETE FROM boards")
        conn.executemany(
            "INSERT INTO boards (id, ats, slug, api_url, not_found, discovered) VALUES (?, ?, ?, ?, ?, ?)",
            [(b.id, b.ats, b.slug, b.api_url, counts.get(b.id, 0), now) for b in boards],
        )


def discovered_at(conn: sqlite3.Connection) -> datetime | None:
    """When the discovered list was last replaced (every row carries that time), or None when it's empty."""
    try:
        at = conn.execute("SELECT MAX(discovered) AS at FROM boards").fetchone()["at"]
        parsed = None if at is None else datetime.fromisoformat(at)
    except (sqlite3.Error, ValueError, TypeError) as e:
        raise ConfigError(f"cannot read the board list's date ({type(e).__name__})") from None
    if parsed is not None and parsed.tzinfo is None:
        raise ConfigError("the board list's date has no time zone")
    return parsed


def discovered_ats(conn: sqlite3.Connection) -> set[str]:
    """The ATSes the discovered list covers."""
    try:
        return {r["ats"] for r in conn.execute("SELECT DISTINCT ats FROM boards")}
    except sqlite3.Error as e:
        raise ConfigError(f"cannot read the board list ({type(e).__name__})") from None


def live_discovered(conn: sqlite3.Connection) -> list[Board]:
    """Discovered boards short of DEAD_AFTER consecutive 404s. A discovered board's company name is its slug."""
    rows = conn.execute("SELECT ats, slug, api_url FROM boards WHERE not_found < ? ORDER BY id", (DEAD_AFTER,))
    return [Board(ats=r["ats"], slug=r["slug"], company=r["slug"], api_url=r["api_url"]) for r in rows]


def companies(conn: sqlite3.Connection, boards: set[str]) -> dict[str, Company]:
    """The companies looked up for these boards, by board id."""
    rows = conn.execute("SELECT board, name, website FROM companies")
    return {r["board"]: Company(name=r["name"], website=r["website"]) for r in rows if r["board"] in boards}


def save_companies(conn: sqlite3.Connection, found: dict[str, Company], now: str) -> None:
    with conn:
        conn.executemany("INSERT OR REPLACE INTO companies (board, name, website, found) VALUES (?, ?, ?, ?)",
                         [(board, c.name, c.website, now) for board, c in found.items()])


def record_board_outcomes(conn: sqlite3.Connection, read: list[str], not_found: list[str]) -> None:
    """A fully read board resets its 404 count, a 404 adds one; other failures leave it as it is."""
    with conn:
        conn.executemany("UPDATE boards SET not_found = 0 WHERE id = ?", [(b,) for b in read])
        conn.executemany("UPDATE boards SET not_found = not_found + 1 WHERE id = ?", [(b,) for b in not_found])


def cached_results(conn: sqlite3.Connection, stored: StoredPosting, defs: list[TagDef], model_id: str,
                   ) -> dict[str, TagResult]:
    """Tag results still valid for this posting text, definition and model, by tag name."""
    by_hash = {d.hash: d for d in defs}
    rows = conn.execute(
        f"""SELECT def_hash, value, quote, verified, warn FROM tag_results
            WHERE posting_key = ? AND input_hash = ? AND model_id = ? AND def_hash IN ({",".join("?" * len(defs))})""",
        (stored.posting.key, stored.input_hash, model_id, *by_hash),
    )
    results = {}
    for r in rows:
        d = by_hash[r["def_hash"]]
        value = json.loads(r["value"])
        results[d.name] = TagResult(value=tuple(value) if d.multi else value, quote=r["quote"],
                                    verified=bool(r["verified"]), warn=bool(r["warn"]))
    return results


def save_results(conn: sqlite3.Connection, stored: StoredPosting, defs: list[TagDef], model_id: str,
                 results: dict[str, TagResult]) -> None:
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO tag_results
               (posting_key, input_hash, def_hash, model_id, value, quote, verified, warn)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(stored.posting.key, stored.input_hash, d.hash, model_id, json.dumps(results[d.name].value),
              results[d.name].quote, results[d.name].verified, results[d.name].warn) for d in defs],
        )


def deliver(conn: sqlite3.Connection, profile: str, posting_keys: list[str], digest_path: Path) -> None:
    """Record that these postings are in a published digest. Call only after the digest file exists."""
    with conn:
        conn.executemany("INSERT INTO deliveries (profile, posting_key, digest_path) VALUES (?, ?, ?)",
                         [(profile, key, str(digest_path)) for key in posting_keys])


def record_run(conn: sqlite3.Connection, run: RunRecord) -> None:
    with conn:
        conn.execute(
            """INSERT INTO runs (started, finished, boards_read, boards_failed, new, tagged, carried_over, cost,
                                cost_complete)
               VALUES (:started, :finished, :boards_read, :boards_failed, :new, :tagged, :carried_over, :cost,
                       :cost_complete)""",
            vars(run),
        )
