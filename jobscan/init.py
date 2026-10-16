"""`jobscan init`: asks for the search in the terminal, then writes config.toml, the profile and the key file, and
email.toml when the user sets up email. `jobscan email` asks the same email questions for an existing install.

Nothing is written until every answer is in and the install it makes loads; a failure part-way through tries to
remove whatever this invocation created and names anything it couldn't. Email settings are saved only after a test
email is accepted."""
import getpass
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from jobscan import mail, tag
from jobscan.config import (
    EMAIL_FILE,
    KEY_FILE,
    ConfigError,
    CustomTag,
    EmailConfig,
    ModelConfig,
    Profile,
    is_host,
    is_mailbox,
    is_password,
    is_port,
    load_config,
    load_email,
    load_profile,
    profile_toml,
    toml_string,
    usable_key,
)
from jobscan.payload import encodable

CONFIG = """# The model that tags postings: DeepSeek V4 Flash on OpenRouter, pinned to one provider and quantisation.
[model]
base_url = "https://openrouter.ai/api/v1"
slug = "deepseek/deepseek-v4-flash"
provider = "parasail"
quantization = "fp8"
"""
CUSTOM_TAGS_EXAMPLE = """
# Your own tags: uncomment and edit. Each one is asked of every matching posting and marked ⓘ in the digest,
# since no labelled set has checked it.
# [custom_tags.b2b]
# definition = "The company sells to businesses, not consumers."
# values = ["yes", "no", "unknown"]
"""
NEXT_STEPS = """
Next:
  jobscan check                  the install loads as a run would (again after editing the profile).
  jobscan run                    the first run finds the job boards, then reads them all and tags every
                                 matching posting, so it can take over an hour. Later runs tag only new ones.
  jobscan schedule --at 08:00    then a run every day, with a notification when there's something new.

Change your search, or add your own tags, in {profile} (`jobscan --help` shows how)"""
# Titles a plain match would let through for a common search; offered as the starting exclusion list.
NEAR_MISSES = MappingProxyType({
    "product manager": ("Product Marketing", "Project Manager", "Program Manager", "Programme Manager"),
    "account executive": ("Account Manager", "Executive Assistant", "Sales Development"),
})
PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")


@dataclass(frozen=True)
class MailServer:
    host: str
    port: int


GMAIL, ICLOUD = MailServer("smtp.gmail.com", 465), MailServer("smtp.mail.me.com", 587)
MAIL_SERVERS = MappingProxyType({"gmail.com": GMAIL, "googlemail.com": GMAIL,
                                 "icloud.com": ICLOUD, "me.com": ICLOUD, "mac.com": ICLOUD})
# Personal Microsoft accounts take only OAuth sign-in from mail apps, which jobscan doesn't do.
OAUTH_ONLY = frozenset({"outlook.com", "hotmail.com", "live.com", "msn.com"})
APP_PASSWORDS = MappingProxyType({
    "smtp.gmail.com": "make one at myaccount.google.com/apppasswords; needs 2-Step Verification",
    "smtp.mail.me.com": "make one at account.apple.com, under App-Specific Passwords",
})


class InitCancelled(Exception):
    """Ctrl-C or end of input while asking: nothing was written."""


@dataclass(frozen=True)
class Setup:
    config: Path
    profile: Path
    key: Path
    email: Path | None  # None: email skipped, or its test failed
    email_error: mail.MailError | None  # why the test email wasn't accepted


@dataclass(frozen=True)
class File:
    text: str
    mode: int


@dataclass(frozen=True)
class Answers:
    name: str
    titles: list[str]
    exclude_titles: list[str]
    locations: list[str]
    eligible_where: str
    tags: list[str]
    key: str = field(repr=False)  # always written to the key file: a scheduled run has no shell environment
    custom_tags: tuple[CustomTag, ...] = ()

    @property
    def profile(self) -> Profile:
        return Profile(name=self.name, titles=tuple(self.titles), exclude_titles=tuple(self.exclude_titles),
                       locations=tuple(self.locations), eligible_where=self.eligible_where, tags=tuple(self.tags),
                       custom_tags=self.custom_tags)


def init(home: Path) -> Setup:
    """Asks, writes, and returns the files written. Raises ConfigError when the install exists already or a file
    can't be written, InitCancelled on Ctrl-C."""
    _refuse_existing(home)
    try:
        answers = _ask()
        email_toml, email_error = None, None
        if _answer("\nEmail each digest to you, from your own mail account? [y/n]", "n",
                   lambda a: a in ("y", "n")) == "y":
            try:
                email_toml = email_setup(answers.name)
            except mail.MailError as e:
                email_error = e
    except (KeyboardInterrupt, EOFError):
        raise InitCancelled from None
    return _install(home, answers, email_toml, email_error)


def init_from(home: Path, answers: Answers) -> Setup:
    """The install `init` writes for these answers, asking nothing (email is `jobscan email`'s). Raises ConfigError
    for an answer the questions wouldn't accept, an existing install, or a file that can't be written."""
    _refuse_existing(home)
    return _install(home, answers, None, None)


def validate(answers: Answers) -> None:
    """What the questions accept, for answers that didn't come through them: the profile, then the key a new install
    needs (its default model is OpenRouter's)."""
    validate_profile(answers.profile)
    if not usable_key(answers.key):
        raise ConfigError("that isn't an OpenRouter key: empty, or it holds spaces or control characters")


def validate_profile(profile: Profile) -> None:
    """A profile `load_profile` and `tag.definitions` accept, checked before anything is written; the name before
    any path is built from it. Custom tags may stand in for built-in ones."""
    if not PROFILE_NAME.fullmatch(profile.name):
        raise ConfigError(f"profile name {profile.name!r}: use lower-case letters, digits and -, starting with a "
                          "letter or digit")
    for label, values in [("titles", profile.titles), ("locations", profile.locations),
                          ("tags", profile.tags + tuple(c.name for c in profile.custom_tags))]:
        if not values:
            raise ConfigError(f"no {label} given")
    for label, values in [("titles", profile.titles), ("excluded titles", profile.exclude_titles),
                          ("locations", profile.locations), ("tags", profile.tags),
                          ("where you can work from", (profile.eligible_where,))]:
        for value in values:
            if not value.strip() or value != value.strip() or not encodable(value):
                raise ConfigError(f"{label}: {value!r} is empty, padded with spaces or not valid text")
    unknown = [t for t in profile.tags if t not in tag.builtin_names()]
    if unknown:
        raise ConfigError(f"unknown tags {unknown}; `jobscan tags` lists them")
    names = [*profile.tags, *(c.name for c in profile.custom_tags)]
    twice = sorted({n for n in names if names.count(n) > 1})
    if twice:
        raise ConfigError(f"tags: {', '.join(twice)} given twice")
    tag.definitions(profile.tags, profile.eligible_where, profile.custom_tags)


def _refuse_existing(home: Path) -> None:
    existing = [p for p in [home / "config.toml", home / KEY_FILE, home / EMAIL_FILE,
                            *sorted((home / "profiles").glob("*.toml"))] if p.exists()]
    if existing:
        raise ConfigError(f"{home} is already set up ({existing[0].name} exists); edit its files instead")


def _install(home: Path, answers: Answers, email_toml: str | None, email_error: mail.MailError | None) -> Setup:
    validate(answers)
    setup = Setup(config=home / "config.toml", profile=home / "profiles" / f"{answers.name}.toml",
                  key=home / KEY_FILE, email=home / EMAIL_FILE if email_toml else None, email_error=email_error)
    files = {setup.config: File(CONFIG, 0o644), setup.profile: File(profile_text(answers), 0o644),
             setup.key: File(answers.key + "\n", 0o600)}
    if setup.email:
        files[setup.email] = File(email_toml, 0o600)
    try:
        _check(answers, home, files)
    except OSError as e:
        raise ConfigError(f"could not check the new install in a temporary directory ({type(e).__name__})") from None
    _write(home, files)
    return setup


def email_setup(profile: str) -> str:
    """Asks for the mail account and sends a test email through it; returns email.toml's text. Raises
    mail.MailError when the test email isn't accepted, ConfigError when the settings can't be checked."""
    email = _ask_email()
    print(f"Sending a test email to {email.to} ...")
    return email_from(email, profile)


def email_from(email: EmailConfig, profile: str) -> str:
    """Checks the settings load exactly as `run` will read them, then sends the test email; returns email.toml's
    text. Raises as `email_setup` does."""
    text = email_text(email)
    try:
        checked = _check_email(text)
    except OSError as e:
        raise ConfigError(f"could not check the email settings in a temporary directory ({type(e).__name__})") \
            from None
    mail.send_test(checked, profile)
    return text


def oauth_only(sender: str) -> bool:
    """Personal Microsoft accounts: mail apps may sign in only with OAuth, which jobscan doesn't do."""
    return sender.rpartition("@")[2].lower() in OAUTH_ONLY


def preset(sender: str) -> MailServer | None:
    """The mail server of a provider jobscan knows, from the sending address."""
    return MAIL_SERVERS.get(sender.rpartition("@")[2].lower())


def app_password(server: MailServer, typed: str) -> str:
    """The password as the server takes it (Google shows its in groups of four, spaces it doesn't want); ConfigError
    when it can't be sent."""
    password = typed.replace(" ", "") if server.host.lower() == GMAIL.host else typed
    if not is_password(password):
        raise ConfigError("that isn't an app password jobscan can send (empty, or control characters)")
    return password


def email_config(to: str, sender: str | None, host: str | None, port: int | None, typed_password: str) -> EmailConfig:
    """The settings the questions would build from these answers, for flags: the sender defaults to `to`, the server
    to the sender's preset; ConfigError names the first answer refused."""
    sender = to if sender is None else sender
    for flag, value in [("--to", to), ("--from", sender)]:
        if not is_mailbox(value):
            raise ConfigError(f"{flag}: not an email address (see `jobscan email --help`)")
    if oauth_only(sender):
        raise ConfigError(f"{sender}: Outlook.com lets mail apps sign in only with OAuth, which jobscan can't do")
    if (host is None) != (port is None):
        raise ConfigError("give --host and --port together")
    if host is not None and not (is_host(host) and is_port(port)):
        raise ConfigError("--host must be a server name and --port a port number")
    server = MailServer(host, port) if host is not None else preset(sender)
    if server is None:
        raise ConfigError(f"no preset mail server for {sender}: give --host and --port")
    return EmailConfig(to=to, sender=sender, host=server.host, port=server.port,
                       password=app_password(server, typed_password))


def email_text(email: EmailConfig) -> str:
    return "\n".join([
        "# Each digest is emailed from your account. This file holds your app password: keep it readable only by you.",
        f"to = {toml_string(email.to)}",
        f"from = {toml_string(email.sender)}",
        f"host = {toml_string(email.host)}",
        f"port = {email.port}",
        f"password = {toml_string(email.password)}",
        "",
    ])


def save_email(home: Path, text: str) -> Path:
    """Replaces email.toml in one rename: a run reads the old settings or the new, never a mix. The temporary file
    is created readable only by the user, and removed if the save fails."""
    path = home / EMAIL_FILE
    try:
        fd, temp = tempfile.mkstemp(dir=home, prefix=".email-", suffix=".tmp")
    except OSError as e:
        raise ConfigError(f"could not write {path} ({type(e).__name__})") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(temp, path)
    except BaseException as e:  # Ctrl-C included: the temporary file holds the password
        left = _remove([Path(temp)])
        kept = f"; delete {temp}, which holds the password" if left else ""
        if isinstance(e, OSError):
            raise ConfigError(f"could not write {path} ({type(e).__name__}); the previous settings are kept{kept}") \
                from None
        if left:
            e.add_note(f"jobscan email{kept}")
        raise
    return path


def save_profile(home: Path, profile: Profile) -> Path:
    """Replaces an install's profile with `profile`, checked first as `init` checks one (validated, written to a
    scratch home and read back). The previous file is kept as `<name>.toml.prev`, which no run reads; each file is
    replaced in one rename, so a run reads the old profile or the new."""
    validate_profile(profile)
    text = profile_toml(profile) + "\n"
    try:
        with tempfile.TemporaryDirectory() as scratch:
            (Path(scratch) / "profiles").mkdir()
            (Path(scratch) / "profiles" / f"{profile.name}.toml").write_text(text, encoding="utf-8")
            if load_profile(Path(scratch)) != profile:
                raise ConfigError("the profile didn't read back as written")
    except OSError as e:
        raise ConfigError(f"could not check the profile in a temporary directory ({type(e).__name__})") from None
    path = home / "profiles" / f"{profile.name}.toml"
    others = [p.name for p in (home / "profiles").glob("*.toml") if p != path]
    if others:  # one profile per install: a second would stop every run
        raise ConfigError(f"this install's profile is {others[0]}, not {path.name}")
    try:
        previous = path.read_bytes() if path.exists() else None
    except OSError as e:
        raise ConfigError(f"could not read {path} ({type(e).__name__})") from None
    if previous is not None:
        _replace(path.with_name(path.name + ".prev"), previous, 0o644)
    _replace(path, text.encode(), 0o644)
    return path


def save_key(home: Path, key: str) -> Path:
    """Replaces the key file (readable only by you) in one rename."""
    if not usable_key(key):
        raise ConfigError("that isn't an OpenRouter key: empty, or it holds spaces or control characters")
    path = home / KEY_FILE
    _replace(path, (key + "\n").encode(), 0o600)
    return path


def default_model() -> ModelConfig:
    """The model a new install gets: `CONFIG` read by the loader `run` uses, never written."""
    try:
        with tempfile.TemporaryDirectory() as scratch:
            (Path(scratch) / "config.toml").write_text(CONFIG, encoding="utf-8")
            return load_config(Path(scratch), environ={"OPENROUTER_API_KEY": "placeholder"}).model
    except OSError as e:
        raise ConfigError(f"could not read the default settings in a temporary directory ({type(e).__name__})") \
            from None


def _replace(path: Path, data: bytes, mode: int) -> None:
    """`data` at `path` in one rename; the temporary file (created readable only by you) gets `mode` before any data
    is written, and is removed on any failure."""
    try:
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    except OSError as e:
        raise ConfigError(f"could not write {path} ({type(e).__name__})") from None
    replaced = False
    try:
        with os.fdopen(fd, "wb") as f:  # owns the descriptor from here, closing it whatever happens
            os.fchmod(f.fileno(), mode)
            f.write(data)
        os.replace(temp, path)
        replaced = True
    except OSError as e:
        raise ConfigError(f"could not write {path} ({type(e).__name__}); it is unchanged") from None
    finally:
        if not replaced and _remove([Path(temp)]):  # Ctrl-C included
            raise ConfigError(f"could not write {path}; delete {temp}")


def remove_email(home: Path) -> bool:
    """False when no email was set up. Never reads the file, so a broken one can be removed too."""
    path = home / EMAIL_FILE
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as e:
        raise ConfigError(f"could not remove {path} ({type(e).__name__})") from None
    return True


def profile_text(answers: Answers) -> str:
    """The profile, with a commented custom-tag example when it has none of its own."""
    text = profile_toml(answers.profile)
    return text if answers.custom_tags else text + "\n" + CUSTOM_TAGS_EXAMPLE


def _ask() -> Answers:
    print("Set up jobscan. Separate several answers with commas.\n")
    name = _answer("Name for this search (lower-case letters, digits and -)", "my-search",
                   lambda a: PROFILE_NAME.fullmatch(a) is not None)
    titles = _split(_answer("Job titles to look for (e.g. Product Manager, Senior Product Manager)", None, _items))
    misses = [m for t in titles for m in NEAR_MISSES.get(t.lower(), ()) if m not in titles]
    exclude = _answer("Titles to leave out, even if they match (- for none)", ", ".join(misses) or "-",
                      lambda a: a == "-" or _items(a))
    locations = _split(_answer("Locations to keep, as job ads write them (e.g. London, UK, Remote)", None, _items))
    eligible_where = _answer("Where you can work from, in a few words (e.g. the UK, working from London)", None,
                             bool)
    print("\nTags: the model answers each one for every matching posting, quoting the ad.")
    tags = _choose_tags()
    return Answers(name=name, titles=titles, exclude_titles=[] if exclude == "-" else _split(exclude),
                   locations=locations, eligible_where=eligible_where, tags=tags, key=_ask_key())


def _ask_key() -> str:
    """Saved to the key file either way: the scheduled run can't see the shell's OPENROUTER_API_KEY."""
    from_env = os.environ.get("OPENROUTER_API_KEY", "").strip()
    hint = "Enter to save the one in OPENROUTER_API_KEY" if usable_key(from_env) else "from openrouter.ai/keys"
    while True:
        key = getpass.getpass(f"\nOpenRouter API key (hidden; {hint}): ").strip() or from_env
        if usable_key(key):
            return key
        print("  That isn't a key: paste it again, with no spaces.")


def _ask_email() -> EmailConfig:
    to = _answer("Email each digest to", None, is_mailbox)
    same = _answer(f"Send it from {to} too? [y/n]", "y", lambda a: a in ("y", "n")) == "y"
    while True:
        sender = to if same else _answer("Send it from which of your mail accounts", None, is_mailbox)
        if not oauth_only(sender):
            break
        print("  Outlook.com only lets mail apps sign in with OAuth, which jobscan can't do. Use a Gmail or iCloud "
              "address, or another provider's mail server.")
        same = False
    server = preset(sender) or MailServer(
        _answer("Your provider's mail (SMTP) server", f"smtp.{sender.rpartition('@')[2].lower()}", is_host),
        int(_answer("Its port: 465 or 587", "587", lambda a: a.isascii() and a.isdigit() and len(a) <= 5
                    and is_port(int(a)))))
    hint = APP_PASSWORDS.get(server.host.lower(), "your provider's app password for mail apps")
    while True:
        try:
            password = app_password(server, getpass.getpass(f"App password (hidden; {hint}): "))
        except ConfigError:
            print("  That isn't a password jobscan can send: paste it again.")
            continue
        return EmailConfig(to=to, sender=sender, host=server.host, port=server.port, password=password)


def _check_email(text: str) -> EmailConfig:
    """The settings must load exactly as `run` will read them."""
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / EMAIL_FILE
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        email = load_email(Path(scratch))
    if email is None:
        raise ConfigError(f"the email settings didn't read back from {path.name}")
    return email


def _choose_tags() -> list[str]:
    while True:
        tags = [t.name for t in tag.builtins()
                if _answer(f"  {t.name} — {t.summary} [y/n]", "y", lambda a: a in ("y", "n")) == "y"]
        if tags:
            return tags
        print("  Pick at least one; you can add your own tags to the profile afterwards.")


def _answer(question: str, default: str | None, valid) -> str:
    prompt = f"{question}{f' [{default}]' if default else ''}: "
    while True:
        answer = input(prompt).strip() or (default or "")
        if encodable(answer) and valid(answer):
            return answer
        print("  Not a valid answer; try again.")



def _items(answer: str) -> bool:
    return bool(_split(answer))


def _split(answer: str) -> list[str]:
    return [item.strip() for item in answer.split(",") if item.strip()]


def _check(answers: Answers, home: Path, files: dict[Path, File]) -> None:
    """The install must load exactly as answered, through the loaders `run` uses, as a scheduled run sees it: with
    no shell environment."""
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        (root / "profiles").mkdir()
        for path, file in files.items():
            target = root / path.relative_to(home)
            target.write_text(file.text, encoding="utf-8")
            target.chmod(file.mode)
        load_config(root, environ={})
        load_email(root)
        profile = load_profile(root)
    if profile != answers.profile:
        raise ConfigError("the profile didn't read back as written")


def _write(home: Path, files: dict[Path, File]) -> None:
    created: list[Path] = []
    try:
        for directory in (home, home / "profiles"):
            if not directory.exists():
                directory.mkdir()
                created.append(directory)
        for path, file in files.items():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, file.mode)
            created.append(path)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(file.text)
    except BaseException as e:
        left = _remove(created)
        kept = f"delete {', '.join(map(str, left))} before trying again" if left else "nothing was kept"
        if isinstance(e, OSError):
            raise ConfigError(f"could not write {home} ({type(e).__name__}); {kept}") from None
        if left:
            e.add_note(f"jobscan init: {kept}")
        raise


def _remove(created: list[Path]) -> list[Path]:
    """Removes what this invocation created, newest first, trying every path; returns what couldn't be removed."""
    left = []
    for path in reversed(created):
        try:
            path.rmdir() if path.is_dir() else path.unlink()
        except OSError:
            left.append(path)
    return left
