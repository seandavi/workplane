"""Settings: secrets and endpoints from the environment, preferences from TOML."""

from __future__ import annotations

import datetime as dt
import fnmatch
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    """Where and how the host-side runner starts agents. Paths may use ``~``."""

    #: Directories holding existing clones, searched by repo name.
    repo_roots: tuple[str, ...] = ("~/src",)
    #: Worktrees go in <worktree_root>/<repo>/wp-<number>; clones missing locally go in _clones/.
    worktree_root: str = "~/.local/share/workplane/worktrees"
    #: Run logs and omp session files.
    state_dir: str = "~/.local/state/workplane"
    max_concurrent: int = 2
    model: str | None = None
    thinking: str | None = None
    max_time: str = "45m"
    #: Needed by `work run`: an unattended agent cannot answer approval prompts, so the harness
    #: must be told to skip them. Unset on purpose; see SECURITY.md.
    approval_mode: str | None = None


@dataclass(frozen=True, slots=True)
class Config:
    database_path: Path
    github_token: str | None = None
    #: GitHub logins that count as "me": my issues go to the backlog, not the inbox.
    me: tuple[str, ...] = ()
    #: Max items in ready/working/review/blocked. 0 disables the limit.
    wip_limit: int = 15
    #: On import, issues from other people newer than this land in the inbox.
    inbox_window_days: int = 14
    #: Users/orgs whose non-archived repos are synced.
    owners: tuple[str, ...] = ()
    #: Extra repos to sync ("owner/name").
    repos: tuple[str, ...] = ()
    include_forks: bool = False
    sync_interval_seconds: int = 600
    noise_title_patterns: tuple[re.Pattern[str], ...] = ()
    noise_authors: frozenset[str] = frozenset()
    #: area name -> repo glob patterns; first match wins.
    areas: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: IANA zone that decides what "today" is for due dates.
    timezone: str = "UTC"
    #: Origins (scheme://host[:port]) besides the server's own that may POST to it, for a reverse
    #: proxy that rewrites the Host header.
    allowed_origins: tuple[str, ...] = ()
    runner: RunnerConfig = field(default_factory=RunnerConfig)

    def area_for(self, full_name: str) -> str | None:
        name = full_name.lower()
        for area, patterns in self.areas.items():
            if any(fnmatch.fnmatchcase(name, p.lower()) for p in patterns):
                return area
        return None

    def is_noise(self, title: str, author: str | None) -> bool:
        if author and (author in self.noise_authors or author.endswith("[bot]")):
            return True
        return any(p.search(title) for p in self.noise_title_patterns)

    def is_me(self, login: str | None) -> bool:
        return login is not None and login.lower() in {m.lower() for m in self.me}

    @property
    def default_actor(self) -> str:
        return self.me[0] if self.me else "me"

    def today(self) -> dt.date:
        return dt.datetime.now(ZoneInfo(self.timezone)).date()


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def load(path: Path | None = None, **overrides: object) -> Config:
    """Read ``WORKPLANE_CONFIG`` (default ``~/.config/workplane/config.toml``) plus environment."""
    path = path or Path(os.environ.get("WORKPLANE_CONFIG") or _config_home() / "workplane" / "config.toml")
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    gh = data.get("github", {})
    noise = data.get("noise", {})
    values: dict[str, object] = {
        "database_path": Path(os.environ.get("WORKPLANE_DB") or _data_home() / "workplane" / "workplane.db"),
        "github_token": os.environ.get("GITHUB_TOKEN") or None,
        "me": tuple(data.get("me", ())),
        "wip_limit": int(data.get("wip_limit", 15)),
        "inbox_window_days": int(data.get("inbox_window_days", 14)),
        "owners": tuple(gh.get("owners", ())),
        "repos": tuple(gh.get("repos", ())),
        "include_forks": bool(gh.get("include_forks", False)),
        "sync_interval_seconds": int(
            os.environ.get("SYNC_INTERVAL_SECONDS", gh.get("sync_interval_seconds", 600))
        ),
        "noise_title_patterns": tuple(re.compile(p) for p in noise.get("title_patterns", ())),
        "noise_authors": frozenset(noise.get("authors", ())),
        "areas": {k: tuple(v) for k, v in data.get("areas", {}).items()},
        "timezone": str(data.get("timezone", "UTC")),
        "allowed_origins": tuple(data.get("allowed_origins", ())),
        "runner": _runner(data.get("runner", {})),
    }
    values.update(overrides)
    return Config(**values)  # type: ignore[arg-type]


def _runner(data: dict) -> RunnerConfig:
    defaults = RunnerConfig()
    return RunnerConfig(
        repo_roots=tuple(data.get("repo_roots", defaults.repo_roots)),
        worktree_root=str(data.get("worktree_root", defaults.worktree_root)),
        state_dir=str(data.get("state_dir", defaults.state_dir)),
        max_concurrent=int(data.get("max_concurrent", defaults.max_concurrent)),
        model=data.get("model") or None,
        thinking=data.get("thinking") or None,
        max_time=str(data.get("max_time", defaults.max_time)),
        approval_mode=data.get("approval_mode") or None,
    )
