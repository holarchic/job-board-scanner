"""Text for the read-only set-up commands (`tags`, `check`, `preview`, `boards`): typed values in, lines out. The
commands' handlers in `cli` gather the values and print what these return."""
from jobscan import tag
from jobscan.config import Profile

VALIDATED = {"yes": "validated", "uk": "validated for the UK only", "no": "not validated"}
WHERE = "<where you can work from>"  # stands in for `eligible_where` before there is a profile


def tag_list(profile: Profile | None) -> list[str]:
    """Every built-in tag, then the profile's custom ones; `*` marks the tags the profile asks."""
    used = set(profile.tags) if profile else set()
    width = max(len(t.name) for t in tag.builtins())
    lines = ["Built-in tags" + (" (* = in your profile)" if profile else "") + ":"]
    for t in tag.builtins():
        lines.append(f"  {'*' if t.name in used else ' '} {t.name:<{width}}  {t.summary}  [{VALIDATED[t.validated]}]")
    if profile and profile.custom_tags:
        lines.append("Your custom tags (every one is asked; none is validated):")
        lines += [f"  * {c.name:<{width}}  {c.definition.splitlines()[0]}" for c in profile.custom_tags]
    lines.append("`jobscan tags NAME` shows a tag's full definition, as the model reads it.")
    return lines


def tag_detail(name: str, profile: Profile | None) -> list[str] | None:
    """The tag's definition as the model reads it, with its answers; None when no tag has the name."""
    where = profile.eligible_where if profile else WHERE
    custom = {c.name: c for c in profile.custom_tags} if profile else {}
    if name in custom:
        d = tag.definitions((), where, (custom[name],))[0]
        kind = "custom tag, not validated"
    elif name in tag.builtin_names():
        d = tag.definitions((name,), where)[0]
        status = next(t.validated for t in tag.builtins() if t.name == name)
        if status == "uk" and profile:
            status = "yes" if d.validated else "no"  # what the digest will mark, for this profile's place
        kind = f"built-in tag, {VALIDATED[status]}"
    else:
        return None
    answers = "free text" if d.values is None else ", ".join(d.values) + (" (a list of these)" if d.multi else "")
    in_use = "" if profile is None else "; in your profile" if name in profile.tags or name in custom else \
        "; not in your profile"
    return [f"{d.name} ({kind}{in_use})", f"Answers: {answers}" + (f"; default {d.default}" if d.default else ""),
            "", d.definition]
