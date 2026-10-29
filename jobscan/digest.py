"""Markdown digest of a profile's new postings: pure rendering plus a never-overwrite publish."""
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from jobscan.config import Requirement
from jobscan.fetch import ADAPTERS
from jobscan.payload import one_line
from jobscan.store import StoredPosting
from jobscan.tag import ABOUT, TagResult, is_unknown


@dataclass(frozen=True)
class DigestEntry:
    posting: StoredPosting
    tags: dict[str, TagResult]  # in the profile's tag order
    open_roles: int  # open postings on the posting's board, all roles
    website: str | None = None  # the company's site, when its ATS gives one
    watched: bool = False  # the company is on the profile's watchlist: the entry leads the digest, marked WATCHED
    notes: tuple[str, ...] = ()  # the seeker's notes naming the company (`watch.noted`), one line each


@dataclass(frozen=True)
class TagView:
    """What the digest knows of the profile's tags beyond their answers: which answers an entry leaves unsaid."""
    unvalidated: frozenset[str] = frozenset()  # named in the legend when an entry shows one
    quiet: frozenset[str] = frozenset()  # asked, never shown on entries (the agency check); a left-out row names one
    required: frozenset[str] = frozenset()  # named by `require`: an unknown answer shows ❓
    # Required single-valued tags whose requirement names one answer: an entry's definite answer can only be that one.
    settled: frozenset[str] = frozenset()
    defaults: Mapping[str, str] = field(default_factory=dict)  # by tag: an answer equal to its default is unsaid

    def __post_init__(self):
        object.__setattr__(self, "defaults", MappingProxyType(dict(self.defaults)))  # frozen, and no caller's alias


@dataclass(frozen=True)
class Unread:
    """Boards a run couldn't read for a reason that may clear, when they are many (`scan.many_unread`)."""
    boards: int
    of: int  # boards the run tried


@dataclass(frozen=True)
class LeftOut:
    """A tagged posting a required tag rules out (`tag.misses`): listed on one line after the entries."""
    entry: DigestEntry
    misses: tuple[Requirement, ...]


PLAIN = TagView()  # every answer but unknowns said, none marked unvalidated
LEGEND = "Not yet validated against labelled postings:"
WATCHED = "★"
WATCH_LEGEND = f"{WATCHED} marks a company on your watchlist (`watch` in your profile)."
LEFT_OUT_NOTE = ("A required tag answers otherwise for these (`require` in your profile), or the agency check "
                 "says an agency posted them for a client (`include_agencies = true` keeps those).")


def render(profile: str, date: str, entries: list[DigestEntry], carried_over: int, view: TagView = PLAIN,
           left_out: Sequence[LeftOut] = (), waiting: int = 0, unread: Unread | None = None) -> str:
    """`view` decides which answers an entry shows (`_said`); the legend names the unvalidated tags entries show, in
    the tags' order. `waiting`: matches the profile's tagging limits held back for later runs; `unread`, a line under
    the header. Watched entries come first, each group in the order given."""
    entries = [e for e in entries if e.watched] + [e for e in entries if not e.watched]
    watched = sum(e.watched for e in entries)
    header = f"# {profile} — {date} · {len(entries)} new"
    if watched:
        header += f" · {watched} watched"
    if carried_over:
        header += f" · {carried_over} untagged, carried over"
    if waiting:
        header += f" · {waiting} waiting for later runs"
    if left_out:
        header += f" · {len(left_out)} left out"
    blocks = [header]
    if unread:
        blocks.append(f"{unread.boards:,} of {unread.of:,} boards couldn't be read this run, most often the network "
                      "down or the Mac asleep; the next run reads them.")
    if watched or any(left.entry.watched for left in left_out):
        blocks.append(WATCH_LEGEND)
    said = {name for entry in entries for name in _said(entry, view)}
    in_order = dict.fromkeys(name for entry in entries for name in entry.tags)
    if unvalidated := [name for name in in_order if name in said and name in view.unvalidated]:
        blocks.append(f"{LEGEND} {', '.join(unvalidated)}.")
    blocks.extend(_entry(entry, date, view) for entry in entries)
    if left_out:
        blocks.append("\n".join([f"## Left out ({len(left_out)})", LEFT_OUT_NOTE,
                                 *map(_left_out_line, left_out)]))
    return "\n\n".join(blocks) + "\n"


def _entry(entry: DigestEntry, date: str, view: TagView) -> str:
    p = entry.posting.posting
    said = _said(entry, view)
    about = said.pop(ABOUT, None)
    # Stored in UTC; the digest's date is the machine's: a posting new today must not read as yesterday's.
    first_seen = datetime.fromisoformat(entry.posting.first_seen).astimezone().date().isoformat()
    link = [p.url, ADAPTERS[p.ats].DISPLAY_NAME, f"{entry.open_roles} open role{'' if entry.open_roles == 1 else 's'}",
            *([f"first seen {first_seen}"] if first_seen < date else []), *([entry.website] if entry.website else [])]
    return "\n".join([
        f"## {_star(entry)}{p.company} — {p.title} · {p.location or 'location not given'}",
        " · ".join(link),
        *([f"Pay: {p.salary}"] if p.salary else []),
        # A free-text answer can hold line breaks; the About and tags lines stay one line each.
        *([one_line(f"About: {_shown(about)}{_warning(about)}")] if about else []),
        *(one_line(f"Your note: {note}") for note in entry.notes),
        *([one_line(" · ".join(_item(name, result) for name, result in said.items()))] if said else []),
    ])


def _said(entry: DigestEntry, view: TagView) -> dict[str, TagResult]:
    """The answers an entry shows, in its tag order. An unknown shows only for a required tag, whatever the tag's
    default (no `About:` line for one). A definite answer marked ⚠ always shows: it may be why a contrary answer
    survived `require`. Otherwise a settled or default answer never does, nor a quiet tag's."""
    def worth_saying(name: str, result: TagResult) -> bool:
        if is_unknown(result.value):
            return name in view.required
        if result.flagged:
            return True
        return name not in view.settled and result.value != view.defaults.get(name)
    return {name: result for name, result in entry.tags.items()
            if name not in view.quiet and worth_saying(name, result)}


def _item(name: str, result: TagResult) -> str:
    if is_unknown(result.value):
        text = f"❓ {name}"
    elif result.value == "yes":
        text = f"✅ {name}"
    elif result.value == "no":
        text = f"❌ {name}"
    else:
        text = f"{name}: {_shown(result)}"
    return text + _warning(result)


def _left_out_line(left: LeftOut) -> str:
    """One physical line however its parts read: a free-text answer or a title can hold line breaks."""
    p = left.entry.posting.posting
    answers = " · ".join(f"{r.tag}: {_shown(left.entry.tags[r.tag])}" for r in left.misses)
    return one_line(f"- {_star(left.entry)}{p.company} — {p.title} · {p.location or 'location not given'} · "
                    f"{answers} · {p.url}")


def _star(entry: DigestEntry) -> str:
    return f"{WATCHED} " if entry.watched else ""


def _shown(result: TagResult) -> str:
    return (", ".join(result.value) or "unknown") if isinstance(result.value, tuple) else result.value


def _warning(result: TagResult) -> str:
    """Why an answer can't be trusted: its quote isn't in the posting, or it has none where one is needed."""
    if result.quote is not None and not result.verified:
        return " ⚠ quote not in posting"
    return " ⚠ no quote" if result.warn else ""


def publish(directory: Path, date: str, text: str) -> Path:
    """Write `text` as <date>.md, or <date>-2.md, -3, … if taken. An existing digest is never overwritten,
    even one created after the name was chosen: the finished file is hard-linked into place, which fails
    rather than replaces."""
    directory.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=directory, prefix=".digest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        n = 1
        while True:
            path = directory / (f"{date}.md" if n == 1 else f"{date}-{n}.md")
            try:
                os.link(temp, path)
                return path
            except FileExistsError:
                n += 1
    finally:
        os.unlink(temp)
