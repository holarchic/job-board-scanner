"""What the saved profile matches among the database's open postings, counted as the run counts them: one entry per
job, none already sent. A snapshot of what the last run stored; the next run reads every board again first."""
import sqlite3
from dataclasses import dataclass

from jobscan import fetch, scan, store
from jobscan.config import Profile
from jobscan.prefilter import Verdict, verdict
from jobscan.records import Board

COST_PER_POSTING = 0.0005  # USD to tag one posting with the default model, measured (README)


@dataclass(frozen=True)
class Sample:
    company: str
    title: str
    location: str


@dataclass(frozen=True)
class Preview:
    open: int  # open postings stored for the boards the next run reads
    matched: int  # postings passing the titles, places and excluded titles
    new_jobs: int  # what the next run would tag: one per job, none whose job was sent
    sent: int  # matched postings whose job was sent already
    excluded: int  # postings an excluded title leaves out
    new_samples: tuple[Sample, ...]  # newest first
    excluded_samples: tuple[Sample, ...]

    @property
    def cost(self) -> float:
        return self.new_jobs * COST_PER_POSTING


def preview(conn: sqlite3.Connection, profile: Profile, listed: dict[str, Board], examples: int = 10) -> Preview:
    """`listed`: the boards file's boards by id, whose names the samples show, as the run does. Only the boards the
    next run reads count: the listed ones and the live discovered ones."""
    reads = store.next_run_boards(conn, set(listed))
    opened, matched, excluded = 0, [], []
    for brief in store.open_brief(conn):
        if brief.board not in reads:
            continue
        opened += 1
        match verdict(profile, brief.title, brief.location):
            case Verdict.MATCH:
                matched.append(brief)
            case Verdict.EXCLUDED:
                excluded.append(brief)
    delivered = store.delivered_keys(conn, profile.name)
    keep = scan.first_per_job([b.key for b in matched], delivered)
    new = sorted((b for b in matched if b.key in keep), key=lambda b: (b.first_seen, b.key), reverse=True)
    sent_jobs = {fetch.same_job(key) for key in delivered}
    shown = new[:examples] + excluded[:examples]
    companies = store.companies(conn, {b.board for b in shown})

    def sample(brief: store.Brief) -> Sample:
        company = scan.shown_company(listed.get(brief.board), companies.get(brief.board), brief.board.split(":", 1)[1])
        return Sample(company=company, title=brief.title, location=brief.location)

    return Preview(open=opened, matched=len(matched), new_jobs=len(new),
                   sent=sum(1 for b in matched if fetch.same_job(b.key) in sent_jobs), excluded=len(excluded),
                   new_samples=tuple(map(sample, new[:examples])),
                   excluded_samples=tuple(map(sample, excluded[:examples])))
