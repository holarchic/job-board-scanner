"""`jobscan titles`: the model proposes other titles the profile's jobs are posted under (variants) and titles its
word match lets through that are other jobs (near-misses); each is counted against the stored postings in the
profile's places, so the user judges a suggestion by its effect. Nothing is written."""
import http.client
import json
import sqlite3
import urllib.error
from dataclasses import dataclass

from jobscan import store, tag
from jobscan.config import ModelConfig, Profile
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
   "Account Executive": "Account Executive Assistant"; for "Product Manager": "Product Marketing Manager").

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


def suggest(profile: Profile, model: ModelConfig, api_key: str | None) -> Suggested:
    text = PROMPT.format(titles=_listed(profile.titles), excluded=_listed(profile.exclude_titles),
                         where=profile.eligible_where, most=MOST)
    try:
        reply = tag.call(model, api_key, text, SCHEMA, "titles")
    except urllib.error.HTTPError as e:
        e.close()
        raise TitlesError(f"HTTP {e.code}", http_status=e.code) from None
    # The class only, never the text: request errors can echo headers, and the key is one.
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise TitlesError(type(e).__name__) from None
    try:
        data = json.loads(reply.content) if reply.content is not None else None
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict) or not all(isinstance(data.get(k), list) for k in ("variants", "near_misses")):
        raise TitlesError("the reply isn't the two lists", usage=reply.usage)
    own = {t.casefold() for t in profile.titles}
    variants = _clean(data["variants"], own | {t.casefold() for t in profile.exclude_titles})
    near = [n for n in _clean(data["near_misses"], {t.casefold() for t in profile.exclude_titles} | own)
            if not any(term_matches(n, title) for title in profile.titles)]  # it would exclude the search itself
    return Suggested(variants=variants, near_misses=tuple(near), usage=reply.usage)


def count(conn: sqlite3.Connection, profile: Profile, reads: set[str], suggested: Suggested) -> Counted:
    """`reads`: the boards the next run reads; postings elsewhere don't count."""
    adds, leaves_out = dict.fromkeys(suggested.variants, 0), dict.fromkeys(suggested.near_misses, 0)
    for brief in store.open_brief(conn):
        if brief.board not in reads or not in_places(profile, brief.location):
            continue
        match verdict(profile, brief.title, brief.location):
            case Verdict.MATCH:
                for near in suggested.near_misses:
                    leaves_out[near] += term_matches(near, brief.title)
            case Verdict.OTHER:
                # In its places, no title of the profile's matches it: a variant matching it makes it a match
                # unless an excluded title hits it, exactly as `verdict` with the variant added would say.
                hits = [v for v in suggested.variants if term_matches(v, brief.title)]
                if hits and not any(term_matches(x, brief.title) for x in profile.exclude_titles):
                    for variant in hits:
                        adds[variant] += 1
    return Counted(adds=adds, leaves_out=leaves_out)


def _listed(values: tuple[str, ...]) -> str:
    return "; ".join(values) or "(none)"


def _clean(raw: list, taken: set[str]) -> tuple[str, ...]:
    """Printable one-line titles, at most LONGEST characters and MOST of them, none twice (case-folded) and none
    the profile has."""
    kept, seen = [], set(taken)
    for item in raw:
        title = one_line(item) if isinstance(item, str) and encodable(item) else ""
        if title and len(title) <= LONGEST and title.casefold() not in seen:
            seen.add(title.casefold())
            kept.append(title)
    return tuple(kept[:MOST])
