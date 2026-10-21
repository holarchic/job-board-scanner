"""Which companies a profile names, matched against a posting's company without a model: the watchlist that leads
the digest, and the seeker's notes shown on entries."""
from jobscan.config import CompanyNote
from jobscan.prefilter import term_matches


def watched(terms: tuple[str, ...], company: str, board_id: str) -> bool:
    return any(_names(term, company, board_id) for term in terms)


def noted(notes: tuple[CompanyNote, ...], company: str, board_id: str) -> tuple[str, ...]:
    """Every note naming the company, in file order."""
    return tuple(n.note for n in notes if _names(n.name, company, board_id))


def _names(term: str, company: str, board_id: str) -> bool:
    """A term names the company when it is whole words of the company's name, by the title filter's rule ("Acme"
    names "Acme AI", not "Acmeco"), or is the board's slug, case-folded (`ashby:hooli-eu` for "hooli-eu")."""
    return term_matches(term, company) or term.strip().casefold() == board_id.split(":", 1)[1].casefold()
