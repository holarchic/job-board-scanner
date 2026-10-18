"""Records shared across the pipeline: boards and postings, independent of which ATS they came from."""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


class Get(Protocol):
    """An adapter's HTTP request through fetch: GET `url`, or POST `body` to it as JSON (Workday's list is POST-only);
    the body of an HTTP 200 answer, after fetch's retries. Raises on anything else."""

    def __call__(self, url: str, body: bytes | None = None) -> bytes: ...


# The profile's location test: is this place's text in the profile's places? (`prefilter.in_places`: one of its
# locations named, or, for a remote text, a country it names or a region holding one, or no place at all)
Places = Callable[[str], bool]


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
