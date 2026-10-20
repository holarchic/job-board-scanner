"""Config and profile loading: TOML under $JOBSCAN_HOME parsed into typed records, or ConfigError."""
import email.policy
import json
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(Exception):
    """A config or profile problem the user must fix before any work starts (exit 2)."""


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    slug: str
    provider: str | None
    quantization: str | None

    @property
    def id(self) -> str:
        """Everything that decides which weights answer: a cached tag result is valid only for the same id."""
        return "|".join([self.base_url, self.slug, self.provider or "", self.quantization or ""])


@dataclass(frozen=True)
class Config:
    home: Path
    boards_file: Path | None  # boards read alongside the discovered ones: config's, else BOARDS_FILE if present
    model: ModelConfig
    api_key: str | None = field(repr=False)


@dataclass(frozen=True)
class CustomTag:
    """A tag the profile defines for itself; never validated against labelled postings."""
    name: str
    definition: str
    values: tuple[str, ...]
    multi: bool
    default: str | None  # stated to the model; a multi tag's default is the empty list


@dataclass(frozen=True)
class Requirement:
    """A posting whose `tag` definitely answers other than `value` is left out of the digest's entries
    (`tag.misses`)."""
    tag: str
    value: str


@dataclass(frozen=True)
class Profile:
    name: str
    titles: tuple[str, ...]
    exclude_titles: tuple[str, ...]
    locations: tuple[str, ...]
    eligible_where: str
    tags: tuple[str, ...]  # built-in tag names
    custom_tags: tuple[CustomTag, ...]  # tagged after `tags`, in file order
    require: tuple[Requirement, ...] = ()  # in file order; names and values checked by `tag.profile_definitions`
    include_agencies: bool = False  # False: the agency check is asked and its yes leaves a posting out
    max_tag_per_run: int = 500  # postings a run sends to the model at most; the rest wait for later runs
    max_spend_per_run: float = 1.0  # dollars: once a run's known tagging spend reaches it, no posting starts


MAX_TAG_PER_RUN, MAX_SPEND_PER_RUN = Profile.max_tag_per_run, Profile.max_spend_per_run


@dataclass(frozen=True)
class EmailConfig:
    """The user's own mail account, which sends each published digest to `to`; `sender` is also the SMTP login."""
    to: str
    sender: str
    host: str
    port: int  # 465: TLS from the start; any other port: STARTTLS, required
    password: str = field(repr=False)


KEY_FILE = "openrouter.key"
BOARDS_FILE = "boards.json"  # read when config.toml names no boards file; where `boards add` writes then
EMAIL_FILE = "email.toml"
EMAIL_KEYS = frozenset({"to", "from", "host", "port", "password"})
MAILBOX = re.compile(r'[^\s@,:;<>()\[\]\\"]+@[^\s@,:;<>()\[\]\\".]+(?:\.[^\s@,:;<>()\[\]\\".]+)+')
CUSTOM_TAG_NAME = re.compile(r"[a-z][a-z0-9_]*")
CUSTOM_TAG_VALUES = ("yes", "no", "unknown")


DEFAULT_HOME = Path("~/.jobscan")


def jobscan_home() -> Path:
    return Path(os.environ.get("JOBSCAN_HOME") or DEFAULT_HOME).expanduser()


def load_config(home: Path, environ: Mapping[str, str] | None = None) -> Config:
    """`environ` stands in for the process environment: `schedule` checks the config as launchd will run it."""
    environ = os.environ if environ is None else environ
    path = home / "config.toml"
    data = _read_toml(path, missing=f"{home}: no install here; `jobscan init` (or `jobscan ui`) sets one up")
    model = data.get("model")
    if not isinstance(model, dict):
        raise ConfigError(f"{path}: missing [model] table")
    boards_file = _string(data, "boards_file", path, required=False)
    boards_file = home / Path(boards_file).expanduser() if boards_file is not None \
        else home / BOARDS_FILE if (home / BOARDS_FILE).exists() else None
    model_config = ModelConfig(
        base_url=_string(model, "base_url", path).rstrip("/"),
        slug=_string(model, "slug", path),
        provider=_string(model, "provider", path, required=False),
        quantization=_string(model, "quantization", path, required=False),
    )
    try:
        url = urlparse(model_config.base_url)
        url.port  # noqa: B018 -- raises on a malformed port
    except ValueError:
        url = None
    if url is None or url.scheme not in ("http", "https") or not url.hostname or url.query or url.fragment \
            or not model_config.base_url.isprintable() or " " in model_config.base_url:
        raise ConfigError(f"{path}: `model.base_url` must be an http(s) URL with no spaces, query or fragment")
    # The OpenRouter key goes to OpenRouter only, never to another configured endpoint.
    api_key = None
    if url.hostname == "openrouter.ai":
        if url.scheme != "https":
            raise ConfigError(f"{path}: OpenRouter must be reached over https")
        if not (model_config.provider and model_config.quantization):
            raise ConfigError(f"{path}: OpenRouter needs `model.provider` and `model.quantization` pinned")
        api_key = environ.get("OPENROUTER_API_KEY") or _key_file(home / KEY_FILE)
        if not usable_key(api_key):
            # Checked here because a malformed header value would otherwise surface inside exception text.
            raise ConfigError("the OpenRouter key contains whitespace or control characters")
    return Config(home=home, boards_file=boards_file, model=model_config, api_key=api_key)


def key_only_in_environment(home: Path, config: Config) -> bool:
    """Whether a scheduled run, which launchd starts with no shell environment, would find no key although this one
    has it. A key file present but unusable is a ConfigError: the scheduled run would stop on it."""
    if config.api_key is None:
        return False
    try:
        load_config(home, environ={})
    except ConfigError:
        if (home / KEY_FILE).exists():
            raise
        return True
    return False


def usable_key(key: str) -> bool:
    """A key that can go in an HTTP header as it is."""
    return bool(key) and key.isascii() and key.isprintable() and " " not in key


def load_email(home: Path) -> EmailConfig | None:
    """None when the install doesn't email digests. `eval` and `discover` never read it, so a broken file stops only
    what emails. Errors name the field, never the password."""
    path = home / EMAIL_FILE
    try:
        text = _secret_text(path)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: not readable ({type(e).__name__})") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:  # the parser's message can quote the file, and the file holds the password
        raise ConfigError(f"{path}: not valid TOML; fix it, or run `jobscan email` again") from None
    if set(data) != EMAIL_KEYS:
        raise ConfigError(f"{path}: needs exactly {', '.join(sorted(EMAIL_KEYS))}")
    for key in ("to", "from"):
        if not is_mailbox(data[key]):
            raise ConfigError(f"{path}: `{key}` must be one email address, like name@example.com")
    host, port, password = data["host"], data["port"], data["password"]
    if not is_host(host):
        raise ConfigError(f"{path}: `host` must be a server name, like smtp.gmail.com")
    if not is_port(port):
        raise ConfigError(f"{path}: `port` must be a number from 1 to 65535")
    if not is_password(password):
        raise ConfigError(f"{path}: `password` must be the app password, in plain ASCII")
    return EmailConfig(to=data["to"], sender=data["from"], host=host, port=port, password=password)


def is_mailbox(value) -> bool:
    """One bare address that the mail library reads back unchanged: never a group, a list or a display name, so the
    message can't go anywhere but the address written (`a:b@x.com` would be the group `a:` sending to b@x.com)."""
    if not (isinstance(value, str) and value.isascii() and value.isprintable() and MAILBOX.fullmatch(value)):
        return False
    header = email.policy.default.header_factory("To", value)
    return not header.defects and [a.addr_spec for a in header.addresses] == [value]


def is_port(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 65536


def is_host(value) -> bool:
    return isinstance(value, str) and bool(value) and value.isascii() and value.isprintable() and " " not in value


def is_password(value) -> bool:
    """smtplib sends the login as ASCII: anything else would fail at send time, after the run."""
    return isinstance(value, str) and bool(value) and value.isascii() and value.isprintable()


def _secret_text(path: Path) -> str:
    """A file holding a secret: refused when anyone but the owner can read it, as ssh refuses a loose private key;
    the mode is checked on the file that is then read."""
    with path.open("rb") as f:
        if os.fstat(f.fileno()).st_mode & 0o077:
            raise ConfigError(f"{path}: other users can read it; run `chmod 600 {path}`")
        return f.read().decode()


def _key_file(path: Path) -> str:
    """The key a scheduled run reads, since launchd passes no shell environment."""
    try:
        key = _secret_text(path).strip()
    except FileNotFoundError:
        raise ConfigError(f"no OpenRouter key: set OPENROUTER_API_KEY or put the key in {path}") from None
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: not readable ({type(e).__name__})") from None
    if not key:
        raise ConfigError(f"{path}: empty")
    return key


def load_profile(home: Path) -> Profile:
    """The skeleton serves exactly one profile per install."""
    files = sorted((home / "profiles").glob("*.toml"))
    if len(files) != 1:
        hint = ("; a new install: `jobscan init` (or `jobscan ui`); an existing one: write profiles/NAME.toml as "
                "`jobscan --help` shows") if not files else ""
        raise ConfigError(f"{home / 'profiles'}: expected exactly one profile, found {len(files)}{hint}")
    path = files[0]
    data = _read_toml(path)
    custom = _custom_tags(data, path)
    tags = _unique(_strings(data, "tags", path, required=not custom), "tags", path)
    return Profile(
        name=path.stem,
        titles=_strings(data, "titles", path),
        exclude_titles=_strings(data, "exclude_titles", path, required=False),
        locations=_strings(data, "locations", path),
        eligible_where=_string(data, "eligible_where", path),
        tags=tags,
        custom_tags=custom,
        require=_require(data, path),
        include_agencies=_boolean(data, "include_agencies", path),
        **_limits(data, path),
    )


def check_max_tag(value) -> int:
    """`max_tag_per_run` by the one rule the profile file, `init`'s flags and the page share."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100_000:
        raise ConfigError("`max_tag_per_run` must be a whole number from 1 to 100,000")
    return value


def check_max_spend(value) -> float:
    """`max_spend_per_run` in dollars, by the one rule the profile file, `init`'s flags and the page share."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1_000:
        # NaN fails the range test too; infinity fails its upper bound.
        raise ConfigError("`max_spend_per_run` must be a number of dollars over 0 and at most 1,000")
    return float(value)


def _limits(data: dict, path: Path) -> dict:
    try:
        return {"max_tag_per_run": check_max_tag(data.get("max_tag_per_run", MAX_TAG_PER_RUN)),
                "max_spend_per_run": check_max_spend(data.get("max_spend_per_run", MAX_SPEND_PER_RUN))}
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from None


def profile_toml(profile: Profile) -> str:
    """The profile file `load_profile` reads back as `profile`: the one writer, beside the one reader."""
    lines = [f"titles = {toml_list(profile.titles)}", f"exclude_titles = {toml_list(profile.exclude_titles)}",
             f"locations = {toml_list(profile.locations)}", f"eligible_where = {toml_string(profile.eligible_where)}",
             f"tags = {toml_list(profile.tags)}"]
    if profile.require:  # tag names are bare keys (CUSTOM_TAG_NAME, and the built-ins' alike)
        lines.append("require = { " + ", ".join(f"{r.tag} = {toml_string(r.value)}" for r in profile.require) + " }")
    if profile.include_agencies:
        lines.append("include_agencies = true")
    if profile.max_tag_per_run != MAX_TAG_PER_RUN:
        lines.append(f"max_tag_per_run = {profile.max_tag_per_run}")
    if profile.max_spend_per_run != MAX_SPEND_PER_RUN:
        lines.append(f"max_spend_per_run = {profile.max_spend_per_run!r}")  # repr: the float reads back exactly
    for c in profile.custom_tags:
        lines += ["", f"[custom_tags.{c.name}]", f"definition = {toml_string(c.definition)}"]
        if c.values != CUSTOM_TAG_VALUES:
            lines.append(f"values = {toml_list(c.values)}")
        if c.multi:
            lines.append("multi = true")
        if c.default is not None:
            lines.append(f"default = {toml_string(c.default)}")
    return "\n".join(lines)


def toml_string(value: str) -> str:
    """A TOML basic string. JSON escapes quotes, backslashes and C0 controls the way TOML does; non-ASCII stays
    literal (a JSON surrogate-pair escape isn't valid TOML); DEL is the one control JSON leaves raw."""
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def toml_list(values) -> str:
    return "[" + ", ".join(toml_string(v) for v in values) + "]"


def _boolean(data: dict, key: str, path: Path) -> bool:
    value = data.get(key, False)
    if not isinstance(value, bool):
        raise ConfigError(f"{path}: `{key}` must be true or false")
    return value


def _require(data: dict, path: Path) -> tuple[Requirement, ...]:
    table = data.get("require", {})
    if not isinstance(table, dict) or not all(isinstance(v, str) and v.strip() for v in table.values()):
        raise ConfigError(f"{path}: `require` must be a table of tag names to values, like "
                          '{ language_required = "no" }')
    return tuple(Requirement(tag=name, value=value) for name, value in table.items())


def _custom_tags(data: dict, path: Path) -> tuple[CustomTag, ...]:
    tables = data.get("custom_tags", {})
    if not isinstance(tables, dict):
        raise ConfigError(f"{path}: `custom_tags` must be a table of tables")
    return tuple(custom_tag(name, table, path) for name, table in tables.items())


def custom_tag(name: str, table, path: Path | str) -> CustomTag:
    """A custom tag from its TOML table, by the profile's rules: the loader's and the set-up page's; `path` names
    where it came from in errors."""
    where = f"{path}: custom tag `{name}`"
    if not CUSTOM_TAG_NAME.fullmatch(name):
        raise ConfigError(f"{path}: custom tag name {name!r} must be lower-case letters, digits and _, "
                          "starting with a letter")
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table")
    unknown = set(table) - {"definition", "values", "multi", "default"}
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    definition = table.get("definition")
    if not isinstance(definition, str) or not definition.strip():
        raise ConfigError(f"{where}: `definition` must be a non-empty string")
    values = table.get("values", CUSTOM_TAG_VALUES)
    # Replies are compared stripped, so a padded value could never be answered.
    if not isinstance(values, (list, tuple)) or not values \
            or not all(isinstance(v, str) and v.strip() and v == v.strip() for v in values) \
            or len(set(values)) != len(values):
        raise ConfigError(f"{where}: `values` must be a non-empty list of different strings, without surrounding "
                          "spaces")
    multi = table.get("multi", False)
    if not isinstance(multi, bool):
        raise ConfigError(f"{where}: `multi` must be true or false")
    default = table.get("default")
    if default is not None and (multi or default not in values):
        raise ConfigError(f"{where}: `default` must be one of `values`, and a multi tag can't have one")
    return CustomTag(name=name, definition=definition.strip(), values=tuple(values), multi=multi, default=default)


def _read_toml(path: Path, missing: str | None = None) -> dict:
    """The file's TOML; `missing` is the error when there's no file."""
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(missing or f"{path}: not found") from None
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: not readable ({type(e).__name__})") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None


def _string(data: dict, key: str, path: Path, required: bool = True) -> str | None:
    value = data.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}: `{key}` must be a non-empty string")
    return value


def _strings(data: dict, key: str, path: Path, required: bool = True) -> tuple[str, ...]:
    value = data.get(key)
    if value is None and not required:
        return ()
    if not isinstance(value, list) or (required and not value) \
            or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{path}: `{key}` must be a {'non-empty ' if required else ''}list of strings")
    return tuple(value)


def _unique(values: tuple[str, ...], key: str, path: Path) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        raise ConfigError(f"{path}: `{key}` lists a name twice")
    return values
