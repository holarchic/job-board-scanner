"""The whole install loaded as `run` loads it here and as a scheduled run would: what `jobscan check` prints and
what the set-up page confirms after saving, from one check."""
import os
from dataclasses import dataclass
from pathlib import Path

from jobscan import fetch, tag
from jobscan.config import (
    KEY_FILE,
    Config,
    EmailConfig,
    Profile,
    key_only_in_environment,
    load_config,
    load_email,
    load_profile,
)


@dataclass(frozen=True)
class Install:
    config: Config
    profile: Profile
    email: EmailConfig | None
    listed: int | None  # boards in the boards file; None: no boards file
    unrecognised: int  # boards-file entries no adapter knows
    key_source: str  # "none" (no key needed), "environment", "file", or "both"
    key_only_in_environment: bool  # a scheduled run won't find the key


def check(home: Path) -> Install:
    """ConfigError names the first thing that doesn't load: a broken file, a malformed key in this environment, or
    a key file a scheduled run couldn't use."""
    config = load_config(home)
    profile = load_profile(home)
    tag.definitions(profile.tags, profile.eligible_where, profile.custom_tags)
    email = load_email(home)
    listed, unrecognised = fetch.load_boards(config.boards_file) if config.boards_file else (None, 0)
    return Install(config=config, profile=profile, email=email, listed=None if listed is None else len(listed),
                   unrecognised=unrecognised, key_source=_key_source(home, config),
                   key_only_in_environment=key_only_in_environment(home, config))


def _key_source(home: Path, config: Config) -> str:
    if config.api_key is None:
        return "none"
    if os.environ.get("OPENROUTER_API_KEY"):
        return "both" if (home / KEY_FILE).exists() else "environment"
    return "file"
