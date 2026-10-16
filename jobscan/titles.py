"""`jobscan titles`: the model proposes other titles the profile's jobs are posted under (variants) and titles its
word match lets through that are other jobs (near-misses); each is counted against the stored postings in the
profile's places, so the user judges a suggestion by its effect. The profile is never changed; opening the database
can back it up first, as a run would."""
import json
import sqlite3
from dataclasses import dataclass, replace

from jobscan import fetch, store, tag
from jobscan.config import Config, ModelConfig, Profile
from jobscan.lock import run_lock
from jobscan.payload import encodable, one_line
from jobscan.prefilter import Verdict, in_places, term_matches, verdict

MOST = 15  # suggestions per list
LONGEST = 80  # characters in one suggested title
PROMPT = """You help someone set up a job search that matches job titles as whole words, case-insensitively.

Their titles: {titles}
Titles they already leave out: {excluded}
Where they can work from: {where}

1. `variants`: up to {most} other job titles that employers post the same jobs under, which their titles miss as
   words (for "Account Executive": "Sales Executive", "AE"). Real titles only, as job ads write them.
2. `near_misses`: up to {most} titles their titles match as words that are different jobs, to leave out (for
   "Account Executive": "Account Executive Assistant"; for "Product Manager": "Product Manager Intern").

Return JSON with both lists."""
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["variants", "near_misses"],
          "properties": {"variants": {"type": "array", "items": {"type": "string"}},
                         "near_misses": {"type": "array", "items": {"type": "string"}}}}


class TitlesError(Exception):
    """The model call failed or its reply wasn't the two lists. `usage` is the reply's, when one came back (billed)."""

    def __init__(self, reason: str, http_status: int | None = None, usage: tag.Usage | None = None):
        super().__init__(reason)
        self.http_status, self.usage = http_status, usage


@dataclass(frozen=True)
class Suggested:
    variants: tuple[str, ...]
    near_misses: tuple[str, ...]
    usage: tag.Usage


@dataclass(frozen=True)
class Counted:
    """Per suggestion, its own effect on the stored postings in the profile's places."""
    adds: dict[str, int]  # variant → postings it would make matches (exclusions still apply)
    leaves_out: dict[str, int]  # near-miss → current matches excluding it would drop


@dataclass(frozen=True)
class Outcome:
    suggested: Suggested
    counted: Counted | None  # None before any run: no database to count against


def for_home(config: Config, profile: Profile) -> Outcome:
    """Suggestions for `profile` (saved or not), counted against the install's database when it has one. The lock
    is taken and the database opened before the model call, so a run holding it (RunLocked) or a database that won't
    open costs no call."""
    listed = {b.id for b in fetch.load_boards(config.boards_file)[0]} if config.boards_file else set()
    if not store.exists(config.home):
        return Outcome(suggest(profile, config.model, config.api_key), None)
    with run_lock(config.home):
        conn = store.open_db(config.home)
        try:
            suggested = suggest(profile, config.model, config.api_key)
            return Outcome(suggested, count(conn, profile, store.next_run_boards(conn, listed), suggested))
        finally:
            conn.close()


def suggest(profile: Profile, model: ModelConfig, api_key: str | None) -> Suggested:
    text = PROMPT.format(titles=_listed(profile.titles), excluded=_listed(profile.exclude_titles),
                         where=profile.eligible_where, most=MOST)
    try:
        reply = tag.call(model, api_key, text, SCHEMA, "titles")
    except tag.CallFailed as e:  # an HTTP refusal isn't billed; another failure may have been
        raise TitlesError(e.reason, e.http_status, usage=None if e.http_status else e.usage) from None
    try:
        data = json.loads(reply.content) if reply.content is not None else None
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict) or set(data) != {"variants", "near_misses"} or not all(
            isinstance(data[k], list) and all(isinstance(item, str) for item in data[k]) for k in data):
        raise TitlesError("the reply isn't the two lists of titles", usage=reply.usage)
    own = {t.strip().casefold() for t in profile.titles}  # as the filter reads its terms
    excluded_already = {t.strip().casefold() for t in profile.exclude_titles}
    variants = tuple(v for v in _clean(data["variants"], own | excluded_already)
                     if not any(term_matches(title, v) for title in profile.titles))  # matched already
    near = [n for n in _clean(data["near_misses"], excluded_already | own)
            if not any(term_matches(n, title) for title in profile.titles)]  # it would exclude the search itself
    return Suggested(variants=variants, near_misses=tuple(near), usage=reply.usage)


def count(conn: sqlite3.Connection, profile: Profile, reads: set[str], suggested: Suggested) -> Counted:
    """`reads`: the boards the next run reads; postings elsewhere don't count."""
    adds, leaves_out = dict.fromkeys(suggested.variants, 0), dict.fromkeys(suggested.near_misses, 0)
    widened = {v: replace(profile, titles=(*profile.titles, v)) for v in suggested.variants}
    for brief in store.open_brief(conn):
        if brief.board not in reads or not in_places(profile, brief.location):
            continue
        match verdict(profile, brief.title, brief.location):
            case Verdict.MATCH:
                for near in suggested.near_misses:
                    leaves_out[near] += term_matches(near, brief.title)
            case Verdict.OTHER:  # only a variant matching the title can change it; the filter decides the rest
                for variant in (v for v in suggested.variants if term_matches(v, brief.title)):
                    adds[variant] += verdict(widened[variant], brief.title, brief.location) is Verdict.MATCH
    return Counted(adds=adds, leaves_out=leaves_out)


def _listed(values: tuple[str, ...]) -> str:
    return "; ".join(v.strip() for v in values) or "(none)"


def _clean(raw: list[str], taken: set[str]) -> tuple[str, ...]:
    """Printable one-line titles, at most LONGEST characters and MOST of them, none twice (case-folded) and none
    the profile has."""
    kept, seen = [], set(taken)
    for item in raw:
        title = one_line(item) if encodable(item) else ""
        if title and len(title) <= LONGEST and title.casefold() not in seen:
            seen.add(title.casefold())
            kept.append(title)
    return tuple(kept[:MOST])
