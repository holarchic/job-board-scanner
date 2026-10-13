"""Command-line entry point: parses arguments, owns logging, the run lock and exit codes; dispatches to the modules."""
import argparse
import fcntl
import logging
import sys
from datetime import time
from pathlib import Path
from time import monotonic

from jobscan import discover, evaluate, init, mail, notify, scan, schedule, store, tag
from jobscan.config import ConfigError, EmailConfig, jobscan_home, load_config, load_email, load_profile
from jobscan.crawl import CrawlError

EXIT_OK, EXIT_INCOMPLETE, EXIT_USAGE = 0, 1, 2
log = logging.getLogger("jobscan")


class RunLocked(Exception):
    """Another jobscan process holds this $JOBSCAN_HOME."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobscan")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="set up a search: asks in the terminal, writes config, profile and key")
    commands.add_parser("discover", help="rebuild the board list from the newest Common Crawl crawls (monthly)")
    run_parser = commands.add_parser("run", help="fetch → filter → tag → write the profile's digest")
    run_parser.add_argument("--notify", action="store_true",
                            help="macOS: a desktop notification on new postings or a problem")
    schedule_parser = commands.add_parser(
        "schedule", help="macOS: run every day at a set time, even after sleep (launchd); elsewhere: print a cron line")
    when = schedule_parser.add_mutually_exclusive_group(required=True)
    when.add_argument("--at", type=_clock, metavar="HH:MM", help="daily run time, 24-hour clock")
    when.add_argument("--off", action="store_true", help="remove the scheduled run")
    eval_parser = commands.add_parser("eval", help="score the tagging path against a labelled posting set")
    eval_parser.add_argument("set", type=Path, help="JSON list of {id, title, location, description, labels}")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return EXIT_OK if e.code == 0 else EXIT_USAGE
    home = jobscan_home()
    if args.command == "init":
        return _init(home)
    if args.command == "eval":
        return _eval(home, args.set)
    if args.command == "discover":
        return _discover(home)
    if args.command == "schedule":
        return _schedule(home, args.at)
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
        with _run_lock(home):
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
    emailed = _email_digest(email, outcome)
    news = _news(outcome, emailed)
    if notifying and news:
        notify.notify(news)
    return EXIT_OK if outcome.complete and emailed else EXIT_INCOMPLETE


def _email_digest(email: EmailConfig | None, outcome: scan.ScanOutcome) -> bool:
    """False only when there was a digest to email and it couldn't be: the file stays, and no later run resends it."""
    if email is None or outcome.digest_path is None:
        return True
    try:
        text = outcome.digest_path.read_text(encoding="utf-8")
        mail.send(email, mail.digest_message(email, outcome.profile, outcome.new, outcome.carried_over, text))
    except (OSError, UnicodeDecodeError, mail.MailError) as e:
        log.error("email_failed reason=%r", str(e) or type(e).__name__)
        return False
    log.info("email_sent to=%s", email.to)
    return True


def _refused(reason: str, notifying: bool) -> int:
    print(f"jobscan: {reason}", file=sys.stderr)
    if notifying:
        notify.notify(f"jobscan couldn't run: {reason}")
    return EXIT_USAGE


def _news(outcome: scan.ScanOutcome, emailed: bool) -> str | None:
    parts = []
    if outcome.new:
        parts.append(f"{outcome.new} new posting{'' if outcome.new == 1 else 's'} in {outcome.digest_path.parent}")
    if outcome.carried_over:
        parts.append(f"{outcome.carried_over} couldn't be tagged yet; see runs.log")
    if outcome.digest_failed:
        parts.append("the digest couldn't be written; see runs.log")
    if not emailed:
        parts.append("the digest couldn't be emailed; see runs.log")
    return "; ".join(parts) or None


def _clock(text: str) -> time:
    hour, _, minute = text.partition(":")
    if not (hour.isdigit() and minute.isdigit() and len(minute) == 2 and int(hour) < 24 and int(minute) < 60):
        raise argparse.ArgumentTypeError("use HH:MM, 24-hour, e.g. 08:00")
    return time(int(hour), int(minute))


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


def _init(home: Path) -> int:
    try:
        setup = init.init(home)
    except ConfigError as e:
        print(f"jobscan: {e}", file=sys.stderr)
        return EXIT_USAGE
    except init.InitCancelled:
        print("\njobscan: cancelled; nothing was written", file=sys.stderr)
        return EXIT_USAGE
    print(f"\nWrote {setup.config}, {setup.profile} and {setup.key}.")
    print(init.NEXT_STEPS.format(profile=setup.profile))
    return EXIT_OK


def _discover(home: Path) -> int:
    """Needs only a writable home: no model config, key, profile or boards file."""
    try:
        _setup_logging(home)
        with _run_lock(home):
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


class _run_lock:
    """One run per $JOBSCAN_HOME: overlapping runs would double-tag and race delivery."""

    def __init__(self, home: Path):
        self.path = home / ".lock"

    def __enter__(self):
        try:
            self.file = self.path.open("w")
        except OSError as e:
            raise ConfigError(f"{self.path}: not writable ({type(e).__name__})") from None
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RunLocked from None
        return self

    def __exit__(self, *exc):
        self.file.close()
