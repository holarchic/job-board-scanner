"""Markdown digest of a profile's new postings: pure rendering plus a never-overwrite publish."""
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from jobscan.fetch import ADAPTERS
from jobscan.store import StoredPosting
from jobscan.tag import TagResult


@dataclass(frozen=True)
class DigestEntry:
    posting: StoredPosting
    tags: dict[str, TagResult]  # in the profile's tag order
    open_roles: int  # open postings on the posting's board, all roles
    website: str | None = None  # the company's site, when its ATS gives one


UNVALIDATED = "ⓘ"
LEGEND = f"{UNVALIDATED} marks a tag that hasn't been validated against labelled postings."


def render(profile: str, date: str, entries: list[DigestEntry], carried_over: int,
           unvalidated: frozenset[str] = frozenset()) -> str:
    """`unvalidated` names the tags shown with ⓘ; the legend appears only when one of them does."""
    header = f"# {profile} — {date} · {len(entries)} new"
    if carried_over:
        header += f" · {carried_over} untagged, carried over"
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
    return "\n\n".join(blocks) + "\n"


def _tag_line(label: str, result: TagResult) -> str:
    shown = (", ".join(result.value) or "unknown") if isinstance(result.value, tuple) else result.value
    if result.quote is None and not result.warn:  # unknown, empty or the tag's default: nothing to cite
        return f"- {label}: {shown}"
    if result.quote is None:
        return f"- {label}: **{shown}** ⚠ no quote"
    return f'- {label}: **{shown}** — "{result.quote}"' + ("" if result.verified else " ⚠ quote not in posting")


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
