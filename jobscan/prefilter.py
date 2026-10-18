"""Profile filter: title and location text, before any model call."""
import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache

from jobscan import regions
from jobscan.config import Profile
from jobscan.records import Posting


class Verdict(Enum):
    MATCH = "match"
    EXCLUDED = "excluded"  # title and place pass, but an excluded title hits
    OTHER = "other"


def matches(posting: Posting, profile: Profile) -> bool:
    return verdict(profile, posting.title, posting.location) is Verdict.MATCH


def verdict(profile: Profile, title: str, location: str) -> Verdict:
    if not _any_term(profile.titles, title) or not in_places(profile, location):
        return Verdict.OTHER
    return Verdict.EXCLUDED if excluded(profile, title) else Verdict.MATCH


def excluded(profile: Profile, title: str) -> bool:
    """Does one of the profile's excluded titles hit this title?"""
    return _any_term(profile.exclude_titles, title)


def in_places(profile: Profile, text: str) -> bool:
    """Is this place text in the profile's places? The location half of `matches`, also what an adapter reading only
    the profile's places tests a place by (`records.Places`). It is when it names one of the profile's locations; a
    remote place ("Remote") names none. A text saying remote is also in them when it names a country the profile
    names, by any of its names, or a region holding one ("Remote - GB", "Remote - EMEA" for a profile naming the
    United Kingdom), or, with a remote place in the profile, when it names no place at all ("Remote", "Anywhere"). So
    "Remote - US" and "Anywhere in France" are in no UK profile's places."""
    places = _places(profile.locations)
    if _any_term(places.named, text):
        return True
    folded = text.casefold()
    if not REMOTE.search(folded):
        return False
    return bool(places.regions.intersection(REGION.findall(folded)) or (places.remote and _unplaced(folded)))


def term_matches(term: str, text: str) -> bool:
    """One title or location term against text, by the filter's own rule."""
    return _any_term((term,), text)


REMOTE = re.compile(r"(?<!\w)(?:remote|remotely|work from home|wfh|home[- ]based|telecommute|distributed|anywhere"
                    r"|worldwide|world wide)(?!\w)")
# Every region and country name, longest first, so "Eastern Europe" is read whole.
REGION = re.compile("|".join(rf"(?<!\w){re.escape(name)}(?!\w)" for name in regions.NAMES))
# The words of a remote text that name no place: remote and open words, how the work is arranged, time-zone offsets
# ("UTC-6 to UTC+2"; a named zone such as "EST" or "CET" is read as a place), and the filler around them. Numbers
# name no place either.
UNPLACED = frozenset({"remote", "remotely", "work", "working", "from", "home", "wfh", "based", "telecommute",
                      "distributed", "anywhere", "worldwide", "world", "wide", "global", "globally", "international",
                      "hybrid", "fully", "first", "only", "or", "and", "in", "the", "to", "job", "position", "flexible",
                      "friendly", "ok", "utc", "gmt", "time", "zone", "zones", "timezone", "timezones", "tz"})


@dataclass(frozen=True)
class _Places:
    named: tuple[str, ...]  # the locations naming a place
    remote: bool  # a location naming none: "Remote", "Anywhere"
    regions: frozenset[str]  # case-folded names of each country a location names and of the regions holding it


@lru_cache(maxsize=64)  # a run holds one profile; the set-up page a draft per edit
def _places(locations: tuple[str, ...]) -> _Places:
    remote = {t for t in locations if REMOTE.search(t.casefold()) and _unplaced(t.casefold())}
    return _Places(named=tuple(t for t in locations if t not in remote), remote=bool(remote),
                   regions=regions.containing(locations))


def _unplaced(folded: str) -> bool:
    return all(word in UNPLACED or word.isdecimal() for word in re.findall(r"\w+", folded))


def _any_term(terms: tuple[str, ...], text: str) -> bool:
    folded = text.casefold()
    return any(_term_pattern(term).search(folded) for term in terms)


def _term_pattern(term: str) -> re.Pattern:
    # Word-adjacent, not \b: "Remote (UK)" must match itself, "UK" must not hit "Ukraine",
    # and "intern" must not hit "International". Both sides are case-folded ("ß" matches "SS").
    return re.compile(rf"(?<!\w){re.escape(term.strip().casefold())}(?!\w)")
