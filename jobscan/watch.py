"""Which companies a profile names, matched against a posting's company without a model: the watchlist that leads
the digest."""
from jobscan.prefilter import term_matches


def watched(terms: tuple[str, ...], company: str, board_id: str) -> bool:
    """A term names the company when it is whole words of the company's name, by the title filter's rule ("Acme"
    names "Acme AI", not "Acmeco"), or is the board's slug, case-folded (`ashby:hooli-eu` for "hooli-eu")."""
    slug = board_id.split(":", 1)[1].casefold()
    return any(term_matches(term, company) or term.strip().casefold() == slug for term in terms)
