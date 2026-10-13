"""One scan for the install's profile: fetch → store → prefilter → tag → digest."""
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from jobscan import digest, discover, fetch, prefilter, store, tag
from jobscan.config import Config, ConfigError, Profile
from jobscan.records import Board

log = logging.getLogger("jobscan")


@dataclass(frozen=True)
class ScanOutcome:
    profile: str
    new: int
    carried_over: int  # matched but untagged: not delivered, retried next run
    digest_path: Path | None
    digest_failed: bool

    @property
    def complete(self) -> bool:
        return not self.carried_over and not self.digest_failed


def scan(config: Config, profile: Profile, defs: list[tag.TagDef]) -> ScanOutcome:
    started_at = datetime.now(UTC)
    started = _stamp(started_at)
    opening = time.monotonic()
    conn = store.open_db(config.home)
    try:
        checking = time.monotonic()
        discover.refresh_if_stale(conn, started_at)
        log.info("prefetch db_open_seconds=%.1f discovery_check_seconds=%.1f", checking - opening,
                 time.monotonic() - checking)
        fetched = _fetch(conn, config, started)
        matched = [p for p in store.pending(conn, profile.name, fetched.eligible)
                   if prefilter.matches(p.posting, profile)]
        entries, tagged, usage = _tag(conn, matched, defs, config)
        outcome = _digest(conn, config, profile, entries, carried_over=len(matched) - len(entries),
                          unvalidated=frozenset(d.name for d in defs if not d.validated))
        store.record_run(conn, store.RunRecord(
            started=started, finished=_now(), boards_read=fetched.read, boards_failed=fetched.failed,
            new=outcome.new, tagged=tagged, carried_over=outcome.carried_over, cost=usage.cost,
            cost_complete=usage.cost_reported))
        return outcome
    finally:
        conn.close()


@dataclass(frozen=True)
class FetchSummary:
    read: int
    failed: int
    eligible: set[str]  # boards whose postings may be delivered: read, or failing for a reason that may clear


def _fetch(conn: sqlite3.Connection, config: Config, now: str) -> FetchSummary:
    """Fetches and stores every board. A 404 isn't a reason that may clear: that board is gone."""
    read, not_found, transient, postings = [], [], [], 0
    for outcome in fetch.fetch_each(_boards(conn, config)):
        if isinstance(outcome, fetch.BoardFetch):
            store.apply_fetch(conn, outcome.board.id, outcome.postings, now)
            read.append(outcome.board.id)
            postings += len(outcome.postings)
        elif outcome.http_status == 404:
            log.info("board_not_found board=%s", outcome.board.id)
            not_found.append(outcome.board.id)
        else:
            log.warning("board_failed company=%r board=%s reason=%r", outcome.board.company, outcome.board.id,
                        outcome.reason)
            transient.append(outcome.board.id)
    store.record_board_outcomes(conn, read, not_found)
    log.info("fetch_done boards_read=%d boards_not_found=%d boards_failed=%d postings=%d", len(read),
             len(not_found), len(transient), postings)
    return FetchSummary(read=len(read), failed=len(not_found) + len(transient), eligible=set(read) | set(transient))


def _boards(conn: sqlite3.Connection, config: Config) -> list[Board]:
    """The boards file's boards plus the live discovered ones; on the same board the file's entry wins, for its
    company name. File boards are never skipped, however often they 404."""
    listed, skipped = fetch.load_boards(config.boards_file) if config.boards_file else ([], 0)
    discovered = store.live_discovered(conn)
    boards = {b.id: b for b in discovered} | {b.id: b for b in listed}
    if not boards:
        raise ConfigError("no boards to read: discovery found none (see runs.log), and config.toml sets no boards_file")
    log.info("fetch_start boards=%d listed=%d discovered=%d skipped_unrecognised=%d", len(boards), len(listed),
             len(discovered), skipped)
    return list(boards.values())


def _tag(conn: sqlite3.Connection, matched: list[store.StoredPosting], defs: list[tag.TagDef], config: Config,
         ) -> tuple[list[digest.DigestEntry], int, tag.Usage]:
    """Digest entries for every matched posting with a full set of tag results (cached or fresh),
    plus the count tagged this run and its cost. Postings left untagged are simply not in the entries."""
    model_id = config.model.id
    results = {s.posting.key: store.cached_results(conn, s, defs, model_id) for s in matched}
    needed = [s for s in matched if len(results[s.posting.key]) < len(defs)]
    log.info("tag_start matched=%d cached=%d to_tag=%d", len(matched), len(matched) - len(needed), len(needed))
    attempts = tag.tag_all([s.posting for s in needed], defs, config.model, config.api_key)
    for s in needed:
        attempt = attempts.get(s.posting.key)
        if attempt is None:
            continue
        if isinstance(attempt, tag.TagFailure):
            # Auth and billing failures need the operator; anything else may clear by the next run.
            level = logging.ERROR if attempt.http_status in (401, 402, 403) else logging.WARNING
            log.log(level, "tag_failed posting=%s error=%r", s.posting.key, attempt.error)
            continue
        store.save_results(conn, s, defs, model_id, attempt.results)
        results[s.posting.key] = attempt.results
    tagged = sum(1 for a in attempts.values() if isinstance(a, tag.TagSuccess))
    usage = sum((a.usage for a in attempts.values()), tag.Usage())
    if len(attempts) < len(needed):
        log.error("tag_stopped reason=consecutive_failures not_started=%d", len(needed) - len(attempts))
    log.info("tag_done tagged=%d failed=%d cost=%.4f cost_reported=%s reasoning_tokens=%d", tagged,
             len(attempts) - tagged, usage.cost, usage.cost_reported, usage.reasoning_tokens)
    open_roles = store.open_counts(conn, {s.posting.board for s in matched})
    entries = [digest.DigestEntry(posting=s, tags={d.name: results[s.posting.key][d.name] for d in defs},
                                  open_roles=open_roles[s.posting.board])
               for s in matched if len(results[s.posting.key]) == len(defs)]
    return entries, tagged, usage


def _digest(conn: sqlite3.Connection, config: Config, profile: Profile, entries: list[digest.DigestEntry],
            carried_over: int, unvalidated: frozenset[str]) -> ScanOutcome:
    if not entries and not carried_over:
        return ScanOutcome(profile.name, new=0, carried_over=0, digest_path=None, digest_failed=False)
    today = date.today().isoformat()
    text = digest.render(profile.name, today, entries, carried_over, unvalidated)
    try:
        path = digest.publish(config.home / "digests" / profile.name, today, text)
    except OSError as e:
        log.error("digest_write_failed profile=%s error=%r", profile.name, str(e))
        return ScanOutcome(profile.name, new=0, carried_over=carried_over, digest_path=None, digest_failed=True)
    store.deliver(conn, profile.name, [e.posting.posting.key for e in entries], path)
    log.info("digest profile=%s new=%d carried_over=%d path=%s", profile.name, len(entries), carried_over, path)
    return ScanOutcome(profile.name, new=len(entries), carried_over=carried_over, digest_path=path,
                       digest_failed=False)


def _now() -> str:
    return _stamp(datetime.now(UTC))


def _stamp(at: datetime) -> str:
    """The stored form of a time: fixed-width ISO, so stored times sort as text."""
    return at.isoformat(timespec="microseconds")
