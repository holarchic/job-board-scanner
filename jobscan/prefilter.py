"""Profile filter: title and location text, before any model call."""
import re
from enum import Enum

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
    return Verdict.EXCLUDED if _any_term(profile.exclude_titles, title) else Verdict.MATCH


def in_places(profile: Profile, text: str) -> bool:
    """Does this place text name one of the profile's locations? The location half of `matches`, also what an adapter
    reading only the profile's places tests a place by (`records.Places`)."""
    return _any_term(profile.locations, text)


def _any_term(terms: tuple[str, ...], text: str) -> bool:
    folded = text.casefold()
    return any(_term_pattern(term).search(folded) for term in terms)


def _term_pattern(term: str) -> re.Pattern:
    # Word-adjacent, not \b: "Remote (UK)" must match itself, "UK" must not hit "Ukraine",
    # and "intern" must not hit "International". Both sides are case-folded ("ß" matches "SS").
    return re.compile(rf"(?<!\w){re.escape(term.strip().casefold())}(?!\w)")
