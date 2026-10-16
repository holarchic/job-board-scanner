"""The set-up page as values: the form a person fills (`Form`), read from posted fields (`read`, never raising),
checked into a profile (`to_profile`), and the page as HTML (`render`, pure). Secrets typed into the form are read
but never rendered."""
import html
from collections.abc import Mapping
from dataclasses import dataclass, field

from jobscan import init, report, tag
from jobscan.config import ConfigError, EmailConfig, Profile, custom_tag
from jobscan.preview import Preview
from jobscan.titles import Outcome

MOST_CUSTOM = 30  # custom tag rows read from one post
TEXT_LIMIT = 20_000  # characters kept from one field


@dataclass(frozen=True)
class CustomRow:
    name: str = ""
    definition: str = ""
    values: str = ""  # one answer per line; empty: yes, no, unknown
    multi: bool = False
    default: str = ""


@dataclass(frozen=True)
class Form:
    name: str = ""
    titles: str = ""  # one per line, as typed
    exclude_titles: str = ""
    locations: str = ""
    eligible_where: str = ""
    tags: frozenset[str] = frozenset()
    custom: tuple[CustomRow, ...] = ()
    email_to: str = ""
    email_from: str = ""
    email_host: str = ""
    email_port: str = ""
    email_off: bool = False
    add_titles: tuple[str, ...] = ()  # suggestions ticked to add to titles
    add_excludes: tuple[str, ...] = ()  # suggestions ticked to leave out
    key: str = field(default="", repr=False)  # typed in this post; never rendered
    password: str = field(default="", repr=False)


@dataclass(frozen=True)
class View:
    """Everything one page shows."""
    form: Form
    token: str
    fresh: bool  # no install yet: the name is asked and the key required
    key_held: bool  # a key is saved, or typed earlier on this page
    password_held: bool
    email: EmailConfig | None  # saved email settings, shown without the password
    errors: Mapping[str, str] = field(default_factory=dict)  # by field; "" for the page
    notices: tuple[str, ...] = ()
    preview: Preview | None = None
    previewed: bool = False  # Preview pressed: None then means no postings yet
    suggestions: Outcome | None = None


def form_from_profile(profile: Profile, email: EmailConfig | None) -> Form:
    return Form(name=profile.name, titles="\n".join(profile.titles), exclude_titles="\n".join(profile.exclude_titles),
                locations="\n".join(profile.locations), eligible_where=profile.eligible_where,
                tags=frozenset(profile.tags),
                custom=tuple(CustomRow(name=c.name, definition=c.definition,
                                       values="" if c.values == ("yes", "no", "unknown") else "\n".join(c.values),
                                       multi=c.multi, default=c.default or "") for c in profile.custom_tags),
                email_to=email.to if email else "", email_from=email.sender if email else "",
                email_host=email.host if email else "", email_port=str(email.port) if email else "")


def read(fields: Mapping[str, list[str]]) -> Form:
    """The form as posted; anything missing is empty, anything malformed is kept as text for the checks to refuse."""
    def one(name: str) -> str:
        values = fields.get(name) or [""]
        return values[0][:TEXT_LIMIT]

    rows = []
    for i in range(MOST_CUSTOM):
        row = CustomRow(name=one(f"custom-{i}-name").strip(), definition=one(f"custom-{i}-definition"),
                        values=one(f"custom-{i}-values"), multi=one(f"custom-{i}-multi") == "on",
                        default=one(f"custom-{i}-default").strip())
        if row.name or row.definition.strip():
            rows.append(row)
    return Form(name=one("name").strip(), titles=one("titles"), exclude_titles=one("exclude_titles"),
                locations=one("locations"), eligible_where=one("eligible_where").strip(),
                tags=frozenset(t for t in fields.get("tag", []) if t in tag.builtin_names()),
                custom=tuple(rows), email_to=one("email_to").strip(), email_from=one("email_from").strip(),
                email_host=one("email_host").strip(), email_port=one("email_port").strip(),
                email_off=one("email_off") == "on",
                add_titles=tuple(v[:TEXT_LIMIT] for v in fields.get("add_title", [])),
                add_excludes=tuple(v[:TEXT_LIMIT] for v in fields.get("add_exclude", [])),
                key=one("key").strip(), password=one("password"))


def to_profile(form: Form) -> tuple[Profile | None, dict[str, str]]:
    """The profile the form describes, ticked suggestions added, or None with errors by field."""
    errors: dict[str, str] = {}
    custom = []
    for i, row in enumerate(form.custom):
        table = {"definition": row.definition, "multi": row.multi}
        if _lines(row.values):
            table["values"] = list(_lines(row.values))
        if row.default:
            table["default"] = row.default
        try:
            custom.append(custom_tag(row.name, table, f"custom tag {row.name or i + 1}"))
        except ConfigError as e:
            errors[f"custom-{i}"] = str(e)
    profile = Profile(name=form.name, titles=_merged(form.titles, form.add_titles),
                      exclude_titles=_merged(form.exclude_titles, form.add_excludes), locations=_lines(form.locations),
                      eligible_where=form.eligible_where, tags=tuple(sorted(form.tags, key=tag.builtin_names().index)),
                      custom_tags=tuple(custom))
    for name, values, message in [("titles", profile.titles, "Give at least one title."),
                                  ("locations", profile.locations, "Give at least one place."),
                                  ("eligible_where", (profile.eligible_where,) if profile.eligible_where else (),
                                   "Say where you can work from.")]:
        if not values:
            errors[name] = message
    if errors:
        return None, errors
    try:
        init.validate_profile(profile)
    except ConfigError as e:
        return None, {"name" if "profile name" in str(e) else "": str(e)}
    return profile, {}


def _lines(text: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def _merged(text: str, added: tuple[str, ...]) -> tuple[str, ...]:
    kept = list(_lines(text))
    kept += [a for a in added if a.strip() and a.strip().casefold() not in {k.casefold() for k in kept}]
    return tuple(a.strip() for a in kept)


def render(view: View) -> str:
    form, e = view.form, view.errors
    parts = [_HEAD, "<h1>jobscan: your search</h1>"]
    parts += [f'<p class="notice">{_x(n)}</p>' for n in view.notices]
    if "" in e:
        parts.append(f'<p class="error">{_x(e[""])}</p>')
    parts.append(f'<form method="post" action="/"><input type="hidden" name="token" value="{_x(view.token)}">')
    if view.fresh:
        parts.append(_text("name", "Name for this search", form.name, e,
                           "lower-case letters, digits and -, e.g. my-search"))
    else:
        parts.append(f"<p>Search: <b>{_x(form.name)}</b></p><input type=\"hidden\" name=\"name\" "
                     f'value="{_x(form.name)}">')
    parts += [
        _area("titles", "Job titles to look for, one per line", form.titles, e,
              "Each matches as whole words anywhere in a posting's title."),
        _area("exclude_titles", "Titles to leave out, one per line", form.exclude_titles, e,
              "A posting whose title holds one of these is skipped."),
        _area("locations", "Places to keep, one per line", form.locations, e,
              "As job ads write them: London, UK, Remote."),
        _text("eligible_where", "Where you can work from", form.eligible_where, e, "e.g. the UK, working from London"),
        _suggestions(view.suggestions),
        _tags(form, e),
        _custom(form, e),
        _key(view, e),
        _email(view, e),
        '<p class="buttons"><button name="action" value="save">Save</button> '
        '<button name="action" value="preview">Preview matches</button> '
        '<button name="action" value="suggest">Suggest titles</button></p>',
        "<p class=\"hint\">Saving rewrites your profile file; comments you wrote in it by hand aren't kept (the "
        "previous file stays beside it as <code>.toml.prev</code>).</p></form>",
    ]
    if view.previewed:
        lines = report.preview_lines(form.name, view.preview) if view.preview \
            else ["No postings yet: after the first `jobscan run`, Preview counts what your search matches."]
        parts.append("<h2>Preview</h2><pre>" + _x("\n".join(lines)) + "</pre>")
    parts.append("</body></html>")
    return "\n".join(p for p in parts if p)


def _tags(form: Form, e: Mapping[str, str]) -> str:
    rows = []
    for t in tag.builtins():
        checked = " checked" if t.name in form.tags else ""
        rows.append(f'<label><input type="checkbox" name="tag" value="{_x(t.name)}"{checked}> <b>{_x(t.name)}</b>: '
                    f"{_x(t.summary)} <i>({_x(report.VALIDATED[t.validated])})</i></label><br>")
    return "<fieldset><legend>Tags the model answers for every match</legend>" + "".join(rows) + _err("tags", e) \
        + "</fieldset>"


def _custom(form: Form, e: Mapping[str, str]) -> str:
    rows = [*form.custom, CustomRow()][:MOST_CUSTOM]
    out = ["<fieldset><legend>Your own tags</legend><p class=\"hint\">Say what each answer means. Leave the name "
           "empty to remove one; fill the empty row to add one.</p>"]
    for i, row in enumerate(rows):
        multi = " checked" if row.multi else ""
        out.append(
            f'<div class="custom">{_err(f"custom-{i}", e)}'
            f'<label>Name <input name="custom-{i}-name" value="{_x(row.name)}"></label> '
            f'<label>Default <input name="custom-{i}-default" value="{_x(row.default)}"></label> '
            f'<label><input type="checkbox" name="custom-{i}-multi"{multi}> several answers</label><br>'
            f'<label>Definition<br><textarea name="custom-{i}-definition" rows="3">{_x(row.definition)}</textarea>'
            f'</label><br><label>Answers, one per line (empty: yes, no, unknown)<br>'
            f'<textarea name="custom-{i}-values" rows="2">{_x(row.values)}</textarea></label></div>')
    return "".join(out) + "</fieldset>"


def _key(view: View, e: Mapping[str, str]) -> str:
    held = "A key is held: leave this empty to keep it." if view.key_held else \
        "From openrouter.ai/keys." if view.fresh else "Needed only for an OpenRouter model."
    return ('<fieldset><legend>OpenRouter key</legend><label><input type="password" name="key" '
            f'autocomplete="off"></label> <span class="hint">{_x(held)}</span>{_err("key", e)}</fieldset>')


def _email(view: View, e: Mapping[str, str]) -> str:
    f = view.form
    saved = f"Now: digests to {view.email.to} from {view.email.sender}." if view.email else "Now: off."
    held = " An app password is held: leave it empty to keep it." if view.password_held else ""
    off = ' <label><input type="checkbox" name="email_off"> turn email off</label>' if view.email else ""
    return ("<fieldset><legend>Email each digest (optional)</legend>"
            f'<p class="hint">{_x(saved)}{_x(held)} Gmail and iCloud need only the address and an app password '
            "(Gmail: myaccount.google.com/apppasswords; iCloud: account.apple.com). A test email is sent before "
            f"anything is saved.</p>{_err('email', e)}"
            + _text("email_to", "Send digests to", f.email_to, e)
            + _text("email_from", "From (empty: the same address)", f.email_from, e)
            + _text("email_host", "Mail server (empty: Gmail or iCloud's)", f.email_host, e)
            + _text("email_port", "Port (465 or 587)", f.email_port, e)
            + '<label>App password <input type="password" name="password" autocomplete="off"></label>'
            + off + "</fieldset>")


def _suggestions(outcome: Outcome | None) -> str:
    if outcome is None:
        return ""
    s, c = outcome.suggested, outcome.counted
    out = ["<fieldset><legend>Suggested titles</legend><p class=\"hint\">Tick the ones to keep; they're added when "
           "you save." + ("" if c else " Counts come after the first run.") + "</p>"]
    for heading, names, field_name, effect, sign in [
            ("Add to titles", s.variants, "add_title", c and c.adds, "+"),
            ("Leave out", s.near_misses, "add_exclude", c and c.leaves_out, "-")]:
        out.append(f"<p><b>{heading}</b></p>")
        out += [f'<label><input type="checkbox" name="{field_name}" value="{_x(n)}"> {_x(n)}'
                + (f" <i>{sign}{effect[n]}</i>" if effect is not None else "") + "</label><br>"
                for n in (sorted(names, key=lambda n: -effect[n]) if effect is not None else names)]
        if not names:
            out.append("<p class=\"hint\">None suggested.</p>")
    out.append(f'<p class="hint">Model call: {_x(report.cost(s.usage))}.</p></fieldset>')
    return "".join(out)


def _text(name: str, label: str, value: str, e: Mapping[str, str], hint: str = "") -> str:
    hint_html = f' <span class="hint">{_x(hint)}</span>' if hint else ""
    return (f'<p><label>{_x(label)}<br><input name="{name}" value="{_x(value)}" size="50"></label>{hint_html}'
            f"{_err(name, e)}</p>")


def _area(name: str, label: str, value: str, e: Mapping[str, str], hint: str) -> str:
    return (f'<p><label>{_x(label)}<br><textarea name="{name}" rows="5" cols="50">{_x(value)}</textarea></label>'
            f' <span class="hint">{_x(hint)}</span>{_err(name, e)}</p>')


def _err(name: str, e: Mapping[str, str]) -> str:
    return f'<span class="error">{_x(e[name])}</span>' if name in e else ""


def _x(text: str) -> str:
    return html.escape(text, quote=True)


_HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>jobscan</title><style>
body{font:15px/1.4 -apple-system,system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem}
fieldset{margin:1rem 0;border:1px solid #ccc}.hint{color:#666;font-size:13px}.error{color:#b00;display:block}
.notice{background:#eef7ee;padding:.5rem}.custom{border-top:1px dashed #ddd;padding:.5rem 0}
textarea{width:100%}pre{background:#f6f6f6;padding:.5rem;white-space:pre-wrap}
</style></head><body>"""
