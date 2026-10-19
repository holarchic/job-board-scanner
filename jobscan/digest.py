"""Markdown digest of a profile's new postings: pure rendering plus a never-overwrite publish."""
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from jobscan.config import Requirement
from jobscan.fetch import ADAPTERS
from jobscan.payload import one_line
from jobscan.store import StoredPosting
from jobscan.tag import TagResult


@dataclass(frozen=True)
class DigestEntry:
    posting: StoredPosting
    tags: dict[str, TagResult]  # in the profile's tag order
    open_roles: int  # open postings on the posting's board, all roles
    website: str | None = None  # the company's site, when its ATS gives one


@dataclass(frozen=True)
class LeftOut:
    """A tagged posting a required tag rules out (`tag.misses`): listed on one line after the entries."""
    entry: DigestEntry
    misses: tuple[Requirement, ...]


UNVALIDATED = "ⓘ"
LEGEND = f"{UNVALIDATED} marks a tag that hasn't been validated against labelled postings."
LEFT_OUT_NOTE = "A required tag answers otherwise for these (`require` in your profile)."


def render(profile: str, date: str, entries: list[DigestEntry], carried_over: int,
           unvalidated: frozenset[str] = frozenset(), left_out: Sequence[LeftOut] = ()) -> str:
    """`unvalidated` names the tags shown with ⓘ; the legend appears only when one of them does on an entry."""
    header = f"# {profile} — {date} · {len(entries)} new"
    if carried_over:
        header += f" · {carried_over} untagged, carried over"
    if left_out:
        header += f" · {len(left_out)} left out"
    blocks = [header]
    if any(name in unvalidated for entry in entries for name in entry.tags):
        blocks.append(LEGEND)
    for entry in entries:
        p = entry.posting.posting
        blocks.append("\n".join([
            f"## {p.company} — {p.title} · {p.location or 'location not given'}",
            f"{p.url} · {ADAPTERS[p.ats].DISPLAY_NAME} · first seen {entry.posting.first_seen[:10]} · "
            f"{entry.open_roles} open role{'' if entry.open_roles == 1 else 's'}",
            *([f"Company: {p.company} · {entry.website}"] if entry.website else []),
            *(_tag_line(name + (f" {UNVALIDATED}" if name in unvalidated else ""), result)
              for name, result in entry.tags.items()),
        ]))
    if left_out:
        blocks.append("\n".join([f"## Left out ({len(left_out)})", LEFT_OUT_NOTE,
                                 *map(_left_out_line, left_out)]))
    return "\n\n".join(blocks) + "\n"


def _left_out_line(left: LeftOut) -> str:
    """One physical line however its parts read: a free-text answer or a title can hold line breaks."""
    p = left.entry.posting.posting
    answers = " · ".join(f"{r.tag}: {_shown(left.entry.tags[r.tag])}" for r in left.misses)
    return one_line(f"- {p.company} — {p.title} · {p.location or 'location not given'} · {answers} · {p.url}")


def _shown(result: TagResult) -> str:
    return (", ".join(result.value) or "unknown") if isinstance(result.value, tuple) else result.value


def _tag_line(label: str, result: TagResult) -> str:
    shown = _shown(result)
    if result.quote is None and not result.warn:  # unknown, empty or the tag's default: nothing to cite
        return f"- {label}: {shown}"
    if result.quote is None:
        return f"- {label}: **{shown}** ⚠ no quote"
    return f'- {label}: **{shown}** — "{result.quote}"' + (" ⚠ quote not in posting" if result.flagged else "")


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
