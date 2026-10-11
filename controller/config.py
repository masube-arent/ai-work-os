"""Controller configuration: JSON, so Python 3.10 (Ubuntu 22.04) needs nothing extra."""
import json
from pathlib import Path

MAX_WORKERS = 10

DEFAULTS = {
    "repo_dir": None,                 # controller's own clone of the GitHub repository
    "state_dir": None,                # SQLite queue, logs, worktrees (trusted only; mode 0700)
    "workspaces_dir": None,           # agent workspaces; default <state_dir>/workspaces. Must be outside a 0700 state_dir when agent_user is set
    "code_dir": None,                 # trusted AI WORK OS checkout whose scripts/ are run; default: this package's repo
    "remote": "origin",
    "base_branch": "main",
    "task_globs": ["examples/*/task*.json", "tasks/*.json", "tasks/*/*.json"],
    "workers": 10,
    "model_concurrency": 2,           # model processes (worker + reviewer runs) at once, across all workers
    "poll_interval_s": 10,
    "sync_interval_s": 120,
    "lease_ttl_s": 600,               # a lease not renewed for this long is reclaimed
    "heartbeat_s": 10,
    "stage_timeout_s": {"work": 900, "check": 600, "review": 600, "judge": 120, "resume": 60},
    "kill_grace_s": 30,
    "max_ai_runs_per_task": 6,        # model runs (work + review) per task, all retries included
    "max_stage_attempts": 3,          # times one stage may be leased (crash/reclaim) before a human looks
    "agent_status_ttl_s": 600,        # how long a CLI login check is trusted
    "auth_recheck_s": 1800,           # wait before re-checking an agent that was not logged in
    "backoff_initial_s": 300,
    "backoff_max_s": 7200,
    "publish": "git",                 # none | git (push branches) | github (push + open PR with gh)
    "agent_bins": {},                 # optional {"claude": path, "codex": path}
    # --- Security Fix 002: OS-level separation of the AI execution user
    "agent_user": None,               # unprivileged user for every AI CLI and AI-written command
    "agent_group": None,              # group shared with the agent user, for workspace trees only
    "agent_python": "/usr/bin/python3",  # interpreter named in the sudoers rule
    "credential_files": [],           # files the agent user must not be able to read (token env file, gh/git config)
    "isolation_check_ttl_s": 600,     # a passed isolation probe is trusted this long
    "github_repo": None,              # owner/name, for the REST API (publish=github)
    "extra_path": [],                 # prepended to PATH for agents (e.g. where codex is installed)
}


class ConfigError(ValueError):
    pass


def load(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"unknown config keys: {sorted(unknown)}")
    cfg = {**DEFAULTS, **data}
    cfg["stage_timeout_s"] = {**DEFAULTS["stage_timeout_s"], **data.get("stage_timeout_s", {})}
    for key in ("repo_dir", "state_dir"):
        if not cfg[key]:
            raise ConfigError(f"{key} is required")
        cfg[key] = str(Path(cfg[key]).expanduser().resolve())
    cfg["workspaces_dir"] = str(Path(cfg["workspaces_dir"]).expanduser().resolve()) if cfg["workspaces_dir"] \
        else str(Path(cfg["state_dir"]) / "workspaces")
    cfg["code_dir"] = str(Path(cfg["code_dir"]).expanduser().resolve()) if cfg["code_dir"] \
        else str(Path(__file__).resolve().parent.parent)
    if not 1 <= int(cfg["workers"]) <= MAX_WORKERS:
        raise ConfigError(f"workers must be 1..{MAX_WORKERS}")
    if not 1 <= int(cfg["model_concurrency"]) <= int(cfg["workers"]):
        raise ConfigError("model_concurrency must be 1..workers")
    if cfg["publish"] not in ("none", "git", "github"):
        raise ConfigError("publish must be none, git or github")
    if bool(cfg["agent_user"]) != bool(cfg["agent_group"]):
        raise ConfigError("agent_user and agent_group go together")
    if cfg["publish"] == "github" and not cfg["github_repo"]:
        raise ConfigError("publish=github needs github_repo (owner/name)")
    if cfg["lease_ttl_s"] <= 3 * cfg["heartbeat_s"]:
        raise ConfigError("lease_ttl_s must be more than 3 x heartbeat_s")
    return cfg
