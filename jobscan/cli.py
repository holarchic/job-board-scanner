"""Command-line entry point: owns logging and exit codes; dispatches each command to the modules."""
import argparse
import logging
import os
import sys
from datetime import time
from pathlib import Path
from time import monotonic

from jobscan import (
    arguments,
    boards,
    discover,
    evaluate,
    fetch,
    init,
    mail,
    notify,
    preview,
    report,
    scan,
    schedule,
    store,
    tag,
    titles,
)
from jobscan.config import (
    EMAIL_FILE,
    KEY_FILE,
    Config,
    ConfigError,
    EmailConfig,
    Profile,
    jobscan_home,
    key_only_in_environment,
    load_config,
    load_email,
    load_profile,
)
from jobscan.crawl import CrawlError
from jobscan.lock import RunLocked, run_lock

EXIT_OK, EXIT_INCOMPLETE, EXIT_USAGE = 0, 1, 2
log = logging.getLogger("jobscan")


def main(argv: list[str] | None = None) -> int:
    parser = arguments.parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return EXIT_OK if e.code == 0 else EXIT_USAGE
    home = jobscan_home()
    if args.command == "init":
        return _init(home, args)
    if args.command == "eval":
        return _eval(home, args.set)
    if args.command == "discover":
        return _discover(home)
    if args.command == "schedule":
        return _schedule(home, args.at)
    if args.command == "email":
        return _email(home, args)
    if args.command == "tags":
        return _tags(home, args.name)
    if args.command == "check":
        return _check(home)
    if args.command == "boards":
        return _boards(home, args)
    if args.command == "preview":
        return _preview(home)
    if args.command == "titles":
        return _titles(home)
    return _run(home, args.notify)


def _run(home: Path, notifying: bool) -> int:
    """With `notifying`, a scheduled run reports news and problems on the desktop; nothing on a quiet run."""
    entered = monotonic()
    try:
        config = load_config(home)
        email = load_email(home)
        profile = load_profile(home)
        defs = tag.definitions(profile.tags, profile.eligible_where, profile.custom_tags)
        _setup_logging(home)
        with run_lock(home):
            log.info("run_start profile=%s setup_seconds=%.1f", profile.name, monotonic() - entered)
            outcome = scan.scan(config, profile, defs)
    except ConfigError as e:
        return _refused(str(e), notifying)
    except RunLocked:
        return _refused(f"another run is using {home}", notifying)
    except Exception as e:
        if notifying:
            notify.notify(f"jobscan stopped with an error ({type(e).__name__}); see {home / 'launchd.log'}")
        raise
    print(f"{outcome.profile}\t{outcome.new}\t{outcome.digest_path or '-'}")
    email_error = _email_digest(email, outcome)
    news = _news(outcome, email_error)
    if notifying and news:
        notify.notify(news)
    return EXIT_OK if outcome.complete and email_error is None else EXIT_INCOMPLETE


def _email_digest(email: EmailConfig | None, outcome: scan.ScanOutcome) -> mail.MailError | None:
    """A digest that couldn't be emailed stays on disk, and no later run resends it."""
    if email is None or outcome.digest_path is None:
        return None
    try:
        mail.send_digest(email, outcome.digest_path, outcome.profile, outcome.new, outcome.carried_over)
    except mail.MailError as e:
        log.error("email_failed reason=%r", str(e))
        return e
    log.info("email_sent")
    return None


def _refused(reason: str, notifying: bool) -> int:
    print(f"jobscan: {reason}", file=sys.stderr)
    if notifying:
        notify.notify(f"jobscan couldn't run: {reason}")
    return EXIT_USAGE


def _news(outcome: scan.ScanOutcome, email_error: mail.MailError | None) -> str | None:
    parts = []
    if outcome.new:
        parts.append(f"{outcome.new} new posting{'' if outcome.new == 1 else 's'} in {outcome.digest_path.parent}")
    if outcome.carried_over:
        parts.append(f"{outcome.carried_over} couldn't be tagged yet; see runs.log")
    if outcome.digest_failed:
        parts.append("the digest couldn't be written; see runs.log")
    if email_error:
        parts.append("the digest couldn't be emailed; see runs.log")
    return "; ".join(parts) or None


def _schedule(home: Path, at: time | None) -> int:
    """`at` None: remove. A schedule is installed only for a config that loads as the job will run it: with no
    shell environment, so the key must be in the key file."""
    if at is not None:
        try:
            load_config(home, environ={})
            load_email(home)
            profile = load_profile(home)
            tag.definitions(profile.tags, profile.eligible_where, profile.custom_tags)
        except ConfigError as e:
            print(f"jobscan: {e}", file=sys.stderr)
            return EXIT_USAGE
    if sys.platform != "darwin":
        print(schedule.cron_line(home, at) if at else "jobscan: remove the jobscan line with `crontab -e`")
        return EXIT_OK
    agents = Path.home() / "Library" / "LaunchAgents"
    try:
        if at is None:
            job = schedule.remove(home, agents)
            print(f"unscheduled\t{job.plist}")
        else:
            job = schedule.install(home, at, agents)
            print(f"scheduled\t{at:%H:%M}\t{job.plist}")
    except schedule.ScheduleError as e:
        print(f"jobscan: could not change the schedule: {e}", file=sys.stderr)
        return EXIT_INCOMPLETE
    return EXIT_OK


EMAIL_FLAGS = ("to", "sender", "host", "port", "password_stdin")


def _email(home: Path, args: argparse.Namespace) -> int:
    """Saves the settings only once a test email is accepted; `--off` works however broken the saved file is."""
    flagged = any(_given(getattr(args, flag)) for flag in EMAIL_FLAGS)
    try:
        if args.off:
            if flagged:
                raise ConfigError("--off takes no other flags")
            print(f"removed\t{home / EMAIL_FILE}" if init.remove_email(home) else "jobscan: no email set up")
            return EXIT_OK
        profile = load_profile(home).name
        if flagged:
            email = _email_settings(args)
            print(f"Sending a test email to {email.to} ...")
            text = init.email_from(email, profile)
        elif not sys.stdin.isatty():
            raise ConfigError("no terminal to ask in: give the flags instead (see `jobscan email --help`)")
        else:
            text = init.email_setup(profile)
        init.save_email(home, text)
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    except (KeyboardInterrupt, EOFError):
        print("\njobscan: cancelled; nothing was saved", file=sys.stderr)
        return EXIT_USAGE
    except mail.MailError as e:
        print(f"jobscan: the test email wasn't sent ({e}); nothing was saved", file=sys.stderr)
        return EXIT_INCOMPLETE
    print(f"Sent a test email; saved {home / EMAIL_FILE}. Each digest will be emailed from now on.")
    return EXIT_OK


INIT_FLAGS = ("name", "title", "exclude_title", "location", "eligible_where", "tag", "key_stdin")


def _given(value) -> bool:
    """A flag on the command line: an empty string or a port 0 is given; an unset flag is None, an unset switch
    False, an unset list empty."""
    return value is not None and value is not False and value != []


def _email_settings(args: argparse.Namespace) -> EmailConfig:
    if not args.password_stdin:
        raise ConfigError("the app password comes from stdin: add --password-stdin")
    return init.email_config(args.to, args.sender, args.host, args.port, sys.stdin.readline().rstrip("\r\n"))


def _init(home: Path, args: argparse.Namespace) -> int:
    try:
        if any(_given(getattr(args, flag)) for flag in INIT_FLAGS):
            setup = init.init_from(home, _init_answers(args))
        elif not sys.stdin.isatty():
            raise ConfigError("no terminal to ask in: give the flags instead (see `jobscan init --help`)")
        else:
            setup = init.init(home)
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    except init.InitCancelled:
        print("\njobscan: cancelled; nothing was written", file=sys.stderr)
        return EXIT_USAGE
    print(f"\nWrote {setup.config}, {setup.profile}, {setup.key}" + (f" and {setup.email}." if setup.email else "."))
    if setup.email_error:
        print(f"The test email wasn't sent ({setup.email_error}), so email isn't set up; `jobscan email` tries again.")
    print(init.NEXT_STEPS.format(profile=setup.profile))
    return EXIT_OK


def _init_answers(args: argparse.Namespace) -> init.Answers:
    missing = [f"--{flag.replace('_', '-')}" for flag in ("name", "title", "location", "eligible_where")
               if getattr(args, flag) in (None, [])]
    if missing:
        raise ConfigError(f"missing {', '.join(missing)} (see `jobscan init --help`)")
    if args.key_stdin:
        key = sys.stdin.readline().strip()
    else:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not key:
            raise ConfigError("no OpenRouter key: pipe it in with --key-stdin, or set OPENROUTER_API_KEY")
    return init.Answers(name=args.name, titles=args.title, exclude_titles=args.exclude_title,
                        locations=args.location, eligible_where=args.eligible_where,
                        tags=args.tag or list(tag.builtin_names()), key=key)


def _discover(home: Path) -> int:
    """Needs only a writable home: no model config, key, profile or boards file."""
    try:
        _setup_logging(home)
        with run_lock(home):
            conn = store.open_db(home)
            try:
                outcome = discover.discover(conn)
            finally:
                conn.close()
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    except RunLocked:
        print(f"jobscan: another run is using {home}", file=sys.stderr)
        return EXIT_USAGE
    except CrawlError as e:
        log.error("discover_failed error=%r kept=previous_list", str(e))
        return EXIT_INCOMPLETE
    print(f"discovered\t{outcome.boards}\t{','.join(outcome.crawls)}")
    return EXIT_OK


def _check(home: Path) -> int:
    try:
        config = load_config(home)
        profile = load_profile(home)
        tag.definitions(profile.tags, profile.eligible_where, profile.custom_tags)
        email = load_email(home)
        listed, unrecognised = fetch.load_boards(config.boards_file) if config.boards_file else (None, 0)
        warnings = [f"the key is only in OPENROUTER_API_KEY, which a scheduled run won't see: save it in "
                    f"{home / KEY_FILE}, readable only by you (chmod 600)"] if key_only_in_environment(home, config) \
            else []
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    print("\n".join(report.install(config, profile, email, None if listed is None else len(listed), unrecognised,
                                   _key_source(home, config), warnings)))
    return EXIT_OK


def _key_source(home: Path, config: Config) -> str:
    if config.api_key is None:
        return "none needed"
    if os.environ.get("OPENROUTER_API_KEY"):
        return f"OPENROUTER_API_KEY here, {KEY_FILE} for scheduled runs" if (home / KEY_FILE).exists() \
            else "OPENROUTER_API_KEY"
    return KEY_FILE


def _preview(home: Path) -> int:
    try:
        profile = load_profile(home)
        result = preview.for_home(home, load_config(home), profile)
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    except RunLocked:
        print("jobscan: a run is using the database; preview once it has finished", file=sys.stderr)
        return EXIT_INCOMPLETE
    if result is None:
        print("No postings yet: `jobscan run` fetches them, then `jobscan preview` can count them.")
    else:
        print("\n".join(report.preview_lines(profile.name, result)))
    return EXIT_OK


def _titles(home: Path) -> int:
    try:
        profile = load_profile(home)
        outcome = titles.for_home(home, load_config(home), profile)
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    except RunLocked:
        print("jobscan: a run is using the database; ask for titles once it has finished", file=sys.stderr)
        return EXIT_INCOMPLETE
    except titles.TitlesError as e:
        spent = f"; {report.cost(e.usage)}" if e.usage else ""
        print(f"jobscan: no title suggestions: {e}{spent}", file=sys.stderr)
        return EXIT_INCOMPLETE
    print("\n".join(report.titles_lines(home / "profiles" / f"{profile.name}.toml", outcome.suggested,
                                       outcome.counted)))
    return EXIT_OK


def _boards(home: Path, args: argparse.Namespace) -> int:
    try:
        file = boards.path(load_config(home))
        if args.boards_command == "add":
            outcome = boards.add(file, args.urls, args.company)
        else:
            print("\n".join(report.board_list(file, boards.entries(file))))
            return EXIT_OK
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    print("\n".join(report.boards_added(file, outcome)))
    return EXIT_OK


def _tags(home: Path, name: str | None) -> int:
    try:
        profile = _profile_if_any(home)
        if name is None:
            lines = report.tag_list(tag.builtins(), profile)
        else:
            lines = report.tag_detail(tag.lookup(name, profile.eligible_where if profile else report.WHERE,
                                                 profile.custom_tags if profile else ()), profile)
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    print("\n".join(lines))
    return EXIT_OK


def _profile_if_any(home: Path) -> Profile | None:
    """None before `init` has written a profile; a broken profile is still a ConfigError."""
    return load_profile(home) if any((home / "profiles").glob("*.toml")) else None


def _eval(home: Path, set_path: Path) -> int:
    try:
        config = load_config(home)
        items = evaluate.load_set(set_path)
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    try:
        report = evaluate.run_eval(items, config)
    except OSError as e:
        print(f"jobscan: could not save eval predictions under {home / 'eval'} ({type(e).__name__})", file=sys.stderr)
        return EXIT_INCOMPLETE
    print(evaluate.format_report(report))
    return EXIT_OK


def _setup_logging(home: Path) -> None:
    for old in list(log.handlers):  # main() can run more than once per process (tests)
        log.removeHandler(old)
        old.close()
    try:
        file_handler = logging.FileHandler(home / "runs.log")
    except OSError as e:
        raise ConfigError(f"{home / 'runs.log'}: not writable ({type(e).__name__})") from None
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for handler in (logging.StreamHandler(sys.stderr), file_handler):
        handler.setFormatter(formatter)
        log.addHandler(handler)
    log.setLevel(logging.INFO)
