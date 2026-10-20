"""One scan for the install's profile: fetch → store → prefilter → describe → name → tag → select → digest."""
import logging
import sqlite3
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path

from jobscan import digest, discover, fetch, prefilter, store, tag
from jobscan.config import Config, ConfigError, Profile, Requirement
from jobscan.records import Board, Company, Places

log = logging.getLogger("jobscan")
PROGRESS_EVERY = 100  # a tag_progress line every this many postings, so a long tagging run shows it is alive


@dataclass(frozen=True)
class ScanOutcome:
    profile: str
    new: int
    carried_over: int  # matched but untagged (failed, or the model looked unavailable): retried next run
    digest_path: Path | None
    digest_failed: bool
    waiting: int = 0  # matched, held back by the profile's tagging limits: pending for later runs, not a failure

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
        fetched = _fetch(conn, config, partial(prefilter.in_places, profile), started)
        matched = _one_per_job(conn, profile.name, [p for p in store.pending(conn, profile.name, set(fetched.eligible))
                                                    if prefilter.matches(p.posting, profile)])
        matched = _describe(matched, fetched.eligible)
        companies = _companies(conn, {s.posting.board for s in matched}, fetched.eligible, started)
        matched = [_named(s, fetched.eligible[s.posting.board], companies.get(s.posting.board)) for s in matched]
        tagging = _tag(conn, matched, defs, config, profile, {b: c.website for b, c in companies.items()})
        shown, left_out = _select(tagging.entries, defs, tag.requirements(profile.require, profile.include_agencies))
        named = {*profile.tags, *(c.name for c in profile.custom_tags)}
        outcome = _digest(conn, config, profile, shown, left_out,
                          carried_over=len(matched) - len(tagging.entries) - tagging.waiting, waiting=tagging.waiting,
                          unvalidated=frozenset(d.name for d in defs if not d.validated),
                          quiet=frozenset(d.name for d in defs if d.name not in named))
        store.record_run(conn, store.RunRecord(
            started=started, finished=_now(), boards_read=fetched.read, boards_failed=fetched.failed,
            new=outcome.new, tagged=tagging.tagged, carried_over=outcome.carried_over, cost=tagging.usage.cost,
            cost_complete=tagging.usage.cost_reported))
        return outcome
    finally:
        conn.close()


@dataclass(frozen=True)
class FetchSummary:
    read: int
    failed: int
    eligible: dict[str, Board]  # by id: boards whose postings may be delivered (read, or failing for a reason that
    # may clear), each as this run read it, so a boards-file entry's endpoint wins


def _fetch(conn: sqlite3.Connection, config: Config, places: Places, now: str) -> FetchSummary:
    """Fetches and stores every board (a Workday board read through its place filters stores only the postings in the
    profile's places). A 404 isn't a reason that may clear: that board is gone."""
    boards = _boards(conn, config)
    read, not_found, transient, postings = [], [], [], 0
    for outcome in fetch.fetch_each(list(boards.values()), places):
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
    return FetchSummary(read=len(read), failed=len(not_found) + len(transient),
                        eligible={b: boards[b] for b in read + transient})


def _boards(conn: sqlite3.Connection, config: Config) -> dict[str, Board]:
    """The boards file's boards plus the live discovered ones; on the same board the file's entry wins, for its
    company name. File boards are never skipped, however often they 404."""
    listed, skipped = fetch.load_boards(config.boards_file) if config.boards_file else ([], 0)
    discovered = store.live_discovered(conn)
    boards = {b.id: b for b in discovered} | {b.id: b for b in listed}
    if not boards:
        raise ConfigError("no boards to read: discovery found none (see runs.log), and config.toml sets no boards_file")
    log.info("fetch_start boards=%d listed=%d discovered=%d skipped_unrecognised=%d", len(boards), len(listed),
             len(discovered), skipped)
    return boards


def _one_per_job(conn: sqlite3.Connection, profile: str,
                 matched: list[store.StoredPosting]) -> list[store.StoredPosting]:
    """One posting per job (`fetch.same_job`): of a job's matched postings, the first by key, and none once the job
    was delivered under any key. The others stay pending and are skipped this way each run; when the kept one's board
    goes, the next by key takes over."""
    keep = first_per_job([s.posting.key for s in matched], store.delivered_keys(conn, profile))
    kept = [s for s in matched if s.posting.key in keep]
    if len(kept) < len(matched):
        log.info("twins_skipped postings=%d", len(matched) - len(kept))
    return kept


def first_per_job(keys: Iterable[str], delivered: Iterable[str]) -> set[str]:
    """Of the keys, one per job (`fetch.same_job`): the first by key, and none of a job delivered under any key."""
    done = {fetch.same_job(key) for key in delivered}
    first: dict[str, str] = {}
    for key in keys:
        job = fetch.same_job(key)
        first[job] = min(first.get(job, key), key)
    return {key for job, key in first.items() if job not in done}


def _describe(matched: list[store.StoredPosting], boards: dict[str, Board]) -> list[store.StoredPosting]:
    """The matched postings, each of an ATS whose list carries no descriptions with its description fetched; the
    tag cache keys on that text. A posting whose detail request fails sits this run out: still pending, described
    again next run, and not carried over, since nothing was tried on it."""
    wanted = [s for s in matched if fetch.ADAPTERS[s.posting.ats].describe is not None]
    if not wanted:
        return matched
    described = {}
    for outcome in fetch.describe_all([(boards[s.posting.board], s.posting) for s in wanted]):
        if isinstance(outcome, fetch.Described):
            described[outcome.posting.key] = outcome.posting
        elif outcome.http_status == 404:
            log.info("describe_not_found posting=%s", outcome.posting.key)
        else:
            log.warning("describe_failed posting=%s reason=%r", outcome.posting.key, outcome.reason)
    log.info("describe_done described=%d failed=%d", len(described), len(wanted) - len(described))
    kept = []
    for s in matched:
        if fetch.ADAPTERS[s.posting.ats].describe is None:
            kept.append(s)
        elif (posting := described.get(s.posting.key)) is not None:
            kept.append(store.StoredPosting(posting, store.input_hash(posting), s.first_seen))
    return kept


def _companies(conn: sqlite3.Connection, wanted: set[str], boards: dict[str, Board], now: str) -> dict[str, Company]:
    """The company of each wanted board, as stored or looked up now for boards never looked up; a board whose lookup
    fails is left out, and looked up again the next run it has a matched posting."""
    known = store.companies(conn, wanted)
    missing = [boards[b] for b in sorted(wanted - known.keys())]
    if not missing:
        return known
    found = {}
    for outcome in fetch.company_all(missing):
        if isinstance(outcome, fetch.CompanyFound):
            found[outcome.board.id] = outcome.company
        elif outcome.http_status == 404:
            log.info("company_not_found board=%s", outcome.board.id)
        else:
            log.warning("company_failed board=%s reason=%r", outcome.board.id, outcome.reason)
    store.save_companies(conn, found, now)
    log.info("company_done found=%d failed=%d", len(found), len(missing) - len(found))
    return known | found


def _named(stored: store.StoredPosting, board: Board, company: Company | None) -> store.StoredPosting:
    """The posting under the name it shows: its board's from this run (a boards-file name), else the looked-up one,
    else the slug. Never the stored row's, which is stale when the board failed this run. Neither the tags nor their
    cache read the company."""
    return replace(stored, posting=replace(stored.posting, company=shown_company(board, company, board.slug)))


def shown_company(board: Board | None, looked_up: Company | None, slug: str) -> str:
    """The name a posting shows: a boards-file name (a board whose company isn't its slug), else the looked-up one,
    else the board's slug."""
    if board is not None and board.company != board.slug:
        return board.company
    return looked_up.name if looked_up else slug


@dataclass(frozen=True)
class Tagging:
    entries: list[digest.DigestEntry]  # every matched posting with a full set of results, before `require` selects
    tagged: int  # sent to the model this run and answered
    usage: tag.Usage  # this run's model calls
    waiting: int  # held back by the profile's limits, never sent: pending for later runs


def _tag(conn: sqlite3.Connection, matched: list[store.StoredPosting], defs: list[tag.TagDef], config: Config,
         profile: Profile, websites: dict[str, str | None]) -> Tagging:
    """Digest entries for every matched posting with a full set of tag results (cached or fresh). Postings the cache
    can't answer go to the model in `tag_queue` order, at most `max_tag_per_run` of them, none started once the run's
    known spend reaches `max_spend_per_run`; the rest wait. Postings left untagged are simply not in the entries."""
    model_id = config.model.id
    results = {s.posting.key: store.cached_results(conn, s, defs, model_id) for s in matched}
    needed = tag_queue([s for s in matched if len(results[s.posting.key]) < len(defs)])
    sending = needed[:profile.max_tag_per_run]
    log.info("tag_start matched=%d cached=%d to_tag=%d sending=%d", len(matched), len(matched) - len(needed),
             len(needed), len(sending))
    by_key = {s.posting.key: s for s in sending}
    done, spent = 0, tag.Usage()

    def record(key: str, attempt: tag.TagAttempt) -> None:
        """Each posting is saved as it finishes, so a run stopped part-way keeps what it has paid for."""
        nonlocal done, spent
        done, spent = done + 1, spent + attempt.usage
        if isinstance(attempt, tag.TagFailure):
            # Auth and billing failures need the operator; anything else may clear by the next run.
            level = logging.ERROR if attempt.http_status in (401, 402, 403) else logging.WARNING
            log.log(level, "tag_failed posting=%s error=%r", key, attempt.error)
        else:
            store.save_results(conn, by_key[key], defs, model_id, attempt.results)
            results[key] = attempt.results
        if done % PROGRESS_EVERY == 0:
            log.info("tag_progress done=%d of=%d cost=%.4f", done, len(sending), spent.cost)

    run = tag.tag_all([s.posting for s in sending], defs, config.model, config.api_key, on_attempt=record,
                      budget=profile.max_spend_per_run)
    attempts = run.attempts
    tagged = sum(1 for a in attempts.values() if isinstance(a, tag.TagSuccess))
    usage = sum((a.usage for a in attempts.values()), tag.Usage())
    unstarted = len(sending) - len(attempts)
    if run.stopped == "failures":
        log.error("tag_stopped reason=consecutive_failures not_started=%d", unstarted)
    over_budget = unstarted if run.stopped == "budget" else 0
    waiting = len(needed) - len(sending) + over_budget
    if waiting:
        log.info("tag_limited waiting=%d by=%s over_count=%d over_budget=%d", waiting,
                 "budget" if over_budget else "count", len(needed) - len(sending), over_budget)
    log.info("tag_done tagged=%d failed=%d cost=%.4f cost_reported=%s reasoning_tokens=%d", tagged,
             len(attempts) - tagged, usage.cost, usage.cost_reported, usage.reasoning_tokens)
    open_roles = store.open_counts(conn, {s.posting.board for s in matched})
    entries = [digest.DigestEntry(posting=s, tags={d.name: results[s.posting.key][d.name] for d in defs},
                                  open_roles=open_roles[s.posting.board], website=websites.get(s.posting.board))
               for s in matched if len(results[s.posting.key]) == len(defs)]
    return Tagging(entries, tagged, usage, waiting)


def tag_queue(postings: list[store.StoredPosting]) -> list[store.StoredPosting]:
    """The order postings go to the model: newest first seen first; among postings first seen together (every
    posting of a first run), one per board in turn (boards by id, each board's postings by key), so a limited run
    spreads across companies instead of taking the first boards whole."""
    turn: dict[str, int] = {}
    taken: Counter[tuple[str, str]] = Counter()
    for s in sorted(postings, key=lambda s: s.posting.key):
        group = (s.first_seen, s.posting.board)
        turn[s.posting.key] = taken[group]
        taken[group] += 1
    in_turns = sorted(postings, key=lambda s: (turn[s.posting.key], s.posting.board, s.posting.key))
    return sorted(in_turns, key=lambda s: s.first_seen, reverse=True)  # stable: turns kept within a first seen


def _select(entries: list[digest.DigestEntry], defs: list[tag.TagDef],
            require: tuple[Requirement, ...]) -> tuple[list[digest.DigestEntry], list[digest.LeftOut]]:
    """The entries `require` (`tag.requirements`: the profile's, and the agency check's unless it includes agencies)
    keeps, and the ones it leaves out with what ruled each out."""
    shown, left_out = [], []
    for entry in entries:
        if missed := tag.misses(defs, require, entry.tags):
            left_out.append(digest.LeftOut(entry, missed))
        else:
            shown.append(entry)
    return shown, left_out


def _digest(conn: sqlite3.Connection, config: Config, profile: Profile, entries: list[digest.DigestEntry],
            left_out: list[digest.LeftOut], carried_over: int, waiting: int, unvalidated: frozenset[str],
            quiet: frozenset[str]) -> ScanOutcome:
    """Left-out postings are delivered with the digest that lists them. Alone they publish nothing: they stay pending,
    their tags cached, until a digest has an entry or a carried-over count to report. Waiting postings alone publish
    nothing either; the outcome still counts them."""
    if not entries and not carried_over:
        if left_out:
            log.info("left_out_pending profile=%s postings=%d", profile.name, len(left_out))
        return ScanOutcome(profile.name, new=0, carried_over=0, digest_path=None, digest_failed=False, waiting=waiting)
    today = date.today().isoformat()
    text = digest.render(profile.name, today, entries, carried_over, unvalidated, left_out, quiet, waiting=waiting)
    try:
        path = digest.publish(config.home / "digests" / profile.name, today, text)
    except OSError as e:
        log.error("digest_write_failed profile=%s error=%r", profile.name, str(e))
        return ScanOutcome(profile.name, new=0, carried_over=carried_over, digest_path=None, digest_failed=True,
                           waiting=waiting)
    store.deliver(conn, profile.name, [e.posting.posting.key for e in [*entries, *(o.entry for o in left_out)]], path)
    log.info("digest profile=%s new=%d carried_over=%d waiting=%d left_out=%d path=%s", profile.name, len(entries),
             carried_over, waiting, len(left_out), path)
    return ScanOutcome(profile.name, new=len(entries), carried_over=carried_over, digest_path=path,
                       digest_failed=False, waiting=waiting)


def _now() -> str:
    return _stamp(datetime.now(UTC))


def _stamp(at: datetime) -> str:
    """The stored form of a time: fixed-width ISO, so stored times sort as text."""
    return at.isoformat(timespec="microseconds")
