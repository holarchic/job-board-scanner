"""Profile filter: title and location text, before any model call."""
import re

from jobscan.config import Profile
from jobscan.records import Posting


def matches(posting: Posting, profile: Profile) -> bool:
    return (_any_term(profile.titles, posting.title)
            and not _any_term(profile.exclude_titles, posting.title)
            and in_places(profile, posting.location))


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
