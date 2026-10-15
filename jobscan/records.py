"""Records shared across the pipeline: boards and postings, independent of which ATS they came from."""
from collections.abc import Callable
from dataclasses import dataclass

# An adapter's HTTP GET: the body of an HTTP 200 answer, after fetch's retries; raises on anything else.
Get = Callable[[str], bytes]


@dataclass(frozen=True)
class Board:
    ats: str
    slug: str
    company: str
    api_url: str

    @property
    def id(self) -> str:
        return f"{self.ats}:{self.slug}"


@dataclass(frozen=True)
class Posting:
    key: str  # <ats>:<slug>:<ATS posting id>
    ats: str
    board: str  # Board.id
    company: str
    title: str
    location: str
    url: str
    description: str


@dataclass(frozen=True)
class Company:
    name: str
    website: str | None  # the company's own site, when its ATS gives one


class BoardError(Exception):
    """The board answered, but not with a usable, well-formed job list, a posting's detail or its company."""
