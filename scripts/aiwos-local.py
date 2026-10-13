#!/usr/bin/env python3
"""AI WORK OS local CLI: run the v0.3 controller on this PC (Linux, or WSL2
Ubuntu driven from Windows PowerShell through deploy/windows/aiwos.ps1).

  aiwos-local.py [--config FILE] init --repo-dir D --state-dir D --workspaces-dir D \
                                      --agent-user U --agent-group G
  aiwos-local.py [--config FILE] doctor      checks only; never starts a model
  aiwos-local.py [--config FILE] start       controller in the background
  aiwos-local.py [--config FILE] status [--json]   warns about processes left by a crash
  aiwos-local.py [--config FILE] stop        SIGTERM the controller and wait (never SIGKILL it),
                                             then stop what a crash left running
  aiwos-local.py [--config FILE] sync        fetch and enqueue once; refused while the controller runs
  aiwos-local.py [--config FILE] review TASK the contract's reviewer, controller stopped

Run as the trusted controller user (aiwos-ctl), never as root or as the agent
user. No GitHub Actions, no VPS, no API keys: the AI CLIs use the agent user's
own subscription logins. Every command that can start an AI first proves the
OS isolation (scripts/isolation.py probe, plus the WSL checks in
controller/wsl.py) and refuses otherwise. The local CLI only runs with
publish=none, require_isolation=true, 10 workers, 1 model slot and, for the
real-PC smoke test, only the dummy contract (tasks/DUMMY-0001/task.json with
task_id DUMMY-0001: path and id both checked, also by the controller through
allowed_task_ids); start, stop, sync and review take one lock per state
directory. Based on the audited
aiwos-local-cli.zip (docs/local-cli.md lists what was changed and why).
"""
import argparse
import fcntl
import fnmatch
import json
import os
import pwd
import select
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONFIG_DEFAULT = Path(os.environ.get("AIWOS_CONFIG", "~/.config/aiwos/local.json")).expanduser()
# kept in the state directory, under names no config can take (see config_location_problem)
PID_NAME, LOG_NAME, LOCK_NAME = "local-controller.pid", "local-controller.log", "local-controller.lock"
SURVIVORS_NAME = "local-survivors.json"  # processes a stop could not end; they block start/sync/review
AGENT_STOP_S = 15  # agent-exec.py: SIGTERM to its session, SIGKILL after 10 s
TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")
# the first local runs (real-PC smoke test): these values exactly, and only the
# dummy contract, matched by its path and by its task id
LOCAL_WORKERS, LOCAL_MODELS = 10, 1
SMOKE_TASK_ID = "DUMMY-0001"
LOCAL_TASK_GLOBS = [f"tasks/{SMOKE_TASK_ID}/task.json"]
LOCAL_ALLOWED_TASK_IDS = [SMOKE_TASK_ID]


class Refused(RuntimeError):
    """A safety condition is not met: do nothing and say why."""


def die(msg, code=2):
    print(f"aiwos-local: {msg}", file=sys.stderr)
    return code


def ensure_linux():
    if not sys.platform.startswith("linux") or not Path("/proc").is_dir():
        raise Refused("the v0.3 controller needs Linux /proc and sudo; on Windows run it inside WSL2 "
                      "through deploy/windows/aiwos.ps1")


def verified_layout():
    if not (ROOT / "controller" / "main.py").is_file() or not (ROOT / "scripts" / "aiwos.py").is_file():
        raise Refused(f"{ROOT} is not an AI WORK OS checkout (controller/ and scripts/ missing)")


def current_user():
    return pwd.getpwuid(os.geteuid()).pw_name


def refuse_root():
    if os.geteuid() == 0:
        raise Refused("do not run as root: run as the controller user, e.g. "
                      "sudo -u aiwos-ctl -H python3 scripts/aiwos-local.py ...")


def inside(path, parent):
    path, parent = Path(path).resolve(), Path(parent).resolve()
    return path == parent or path.is_relative_to(parent)


def config_location_problem(cfgpath, cfg):
    """The config must not sit where the controller writes its own files, where
    the agent can reach it, or in a git checkout."""
    cfgpath = Path(cfgpath).resolve()
    places = {"state_dir": cfg.get("state_dir"), "workspaces_dir": cfg.get("workspaces_dir"),
              "repo_dir": cfg.get("repo_dir"), "code_dir": cfg.get("code_dir") or str(ROOT)}
    for key, place in places.items():
        if place and inside(cfgpath, place):
            return f"config {cfgpath} must not be inside {key} ({place})"
    return None


def local_policy(cfg):
    """What the local CLI insists on before anything can run."""
    problems = []
    if cfg.get("publish") != "none":
        problems.append(f"publish={cfg.get('publish')!r}: the local CLI runs with publish=none only")
    if cfg.get("require_isolation") is not True:
        problems.append("require_isolation must be true")
    if cfg.get("workers") != LOCAL_WORKERS:
        problems.append(f"workers={cfg.get('workers')!r}: the local CLI runs with exactly {LOCAL_WORKERS}")
    if cfg.get("model_concurrency") != LOCAL_MODELS:
        problems.append(f"model_concurrency={cfg.get('model_concurrency')!r}: the local CLI runs with exactly "
                        f"{LOCAL_MODELS}")
    if cfg.get("task_globs") != LOCAL_TASK_GLOBS:
        problems.append(f"task_globs={cfg.get('task_globs')!r}: the smoke test queues only {LOCAL_TASK_GLOBS}")
    if cfg.get("allowed_task_ids") != LOCAL_ALLOWED_TASK_IDS:
        problems.append(f"allowed_task_ids={cfg.get('allowed_task_ids')!r}: the smoke test runs only "
                        f"{LOCAL_ALLOWED_TASK_IDS}")
    if Path(cfg.get("code_dir") or ROOT).resolve() != ROOT:
        problems.append(f"code_dir {cfg.get('code_dir')} is not this CLI's checkout {ROOT}")
    if not cfg.get("agent_user") or not cfg.get("agent_group"):
        problems.append("agent_user/agent_group absent: refusing unisolated model execution")
    elif cfg["agent_user"] == current_user():
        problems.append("agent_user must differ from the trusted controller user")
    return problems


def load_cfg(cfgpath):
    from controller.config import load
    cfgpath = Path(cfgpath).expanduser().resolve()
    cfg = load(cfgpath)
    problem = config_location_problem(cfgpath, cfg)
    if problem:
        raise Refused(problem)
    cfg["_config_path"] = str(cfgpath)
    return cfg


def paths(cfg):
    state = Path(cfg["state_dir"])
    return state / PID_NAME, state / LOG_NAME, state / LOCK_NAME


@contextmanager
def single_operator(cfg):
    """One start/stop/review at a time for this state directory."""
    state = Path(cfg["state_dir"])
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    fd = os.open(str(paths(cfg)[2]), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def process_start_ticks(pid):
    """Start time from /proc: tells a live process from a later one reusing its PID."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def controller_pid(cfg):
    """The PID of this config's controller, or None. Only a process whose start
    time and command line both match the pid file counts: a stale pid file must
    never lead to signalling an unrelated process."""
    pidpath = paths(cfg)[0]
    try:
        record = json.loads(pidpath.read_text())
        pid = int(record["pid"])
        if not record.get("start_ticks") or record["start_ticks"] != process_start_ticks(pid):
            return None
        argv = [a.decode(errors="replace") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
        cfgarg = str(Path(record["config"]).resolve())
        expected = ["-m", "controller", "run", "--config", cfgarg]
        if not any(argv[i:i + 5] == expected for i in range(len(argv) - 4)):
            return None
        if os.readlink(f"/proc/{pid}/cwd") != str(ROOT) or process_start_ticks(pid) != record["start_ticks"]:
            return None  # not run from this checkout, or replaced while being read
        if cfgarg != str(Path(cfg["_config_path"]).resolve()):
            return None
        return pid
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def stored_isolation(cfg):
    """The last isolation verdict the controller stored, read-only (no probe)."""
    db = Path(cfg["state_dir"]) / "aiwos.db"
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            row = con.execute("SELECT value, updated_at FROM meta WHERE key='isolation'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    return {**json.loads(row[0]), "checked_at": row[1]}


def boundary_check(cfg):
    """Prove the OS isolation now (as the agent user), or refuse."""
    ensure_linux()
    problems = local_policy(cfg)
    if problems:
        raise Refused("; ".join(problems))
    from controller.main import export_isolation_env, prepare, refresh_isolation
    export_isolation_env(cfg)
    store = prepare(cfg)
    try:
        if not refresh_isolation(cfg, store):
            verdict, _ = store.get_meta("isolation")
            raise Refused("OS isolation not verified, no AI was started: " + str((verdict or {}).get("reason")))
    finally:
        store.db.close()


def run_controller(cfg, action, extra=(), quiet=False):
    return subprocess.run([sys.executable, "-m", "controller", action, "--config", cfg["_config_path"], *extra],
                          cwd=ROOT, check=False, stdout=subprocess.DEVNULL if quiet else None).returncode


# ------------------------------------------------------------------ commands

def cmd_init(args):
    cfgpath = Path(args.config).expanduser().resolve()
    if cfgpath.exists():
        return die(f"config already exists (not overwriting): {cfgpath}")
    from controller.config import DEFAULTS
    repo = Path(args.repo_dir).expanduser().resolve()
    if not (repo / ".git").exists():
        return die("repo_dir must be an existing git clone, separate from the code checkout (git clone first)")
    if inside(repo, ROOT) or inside(ROOT, repo):
        return die("repo_dir must not be (or contain) the trusted AI WORK OS code checkout")
    cfg = {**DEFAULTS}
    cfg.update(repo_dir=str(repo), state_dir=str(Path(args.state_dir).expanduser().resolve()),
               workspaces_dir=str(Path(args.workspaces_dir).expanduser().resolve()), code_dir=str(ROOT),
               agent_user=args.agent_user, agent_group=args.agent_group, workers=LOCAL_WORKERS,
               model_concurrency=LOCAL_MODELS, publish="none", require_isolation=True,
               task_globs=list(LOCAL_TASK_GLOBS), allowed_task_ids=list(LOCAL_ALLOWED_TASK_IDS),
               credential_files=[str(Path(p).expanduser().resolve()) for p in args.credential_file],
               github_repo=args.github_repo)
    problem = config_location_problem(cfgpath, cfg)
    if problem:
        return die(problem)
    if inside(cfg["workspaces_dir"], cfg["state_dir"]):
        return die("workspaces_dir must be outside state_dir (state is 0700, the agent needs the workspaces)")
    problems = local_policy(cfg)
    if problems:
        return die("; ".join(problems))
    cfgpath.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = cfgpath.with_name(cfgpath.name + ".tmp")
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        os.replace(tmp, cfgpath)
    finally:
        tmp.unlink(missing_ok=True)
    print(f"Config created: {cfgpath} (publish=none, require_isolation=true, workers=10, models=1, "
          f"tasks: {SMOKE_TASK_ID} only)")
    print("Next: doctor, then start. No AI runs until the isolation probe passes.")
    return 0


def cmd_doctor(args):
    """Checks only: no controller, no model call. Exit 1 if a required item fails."""
    ok = True

    def line(status, what):
        nonlocal ok
        ok = ok and status != "NG"
        print(f"  [{status}] {what}")

    from controller import wsl
    print("platform")
    line("OK" if sys.version_info >= (3, 9) else "NG", f"python {sys.version.split()[0]} (3.9+ needed)")
    line("OK" if subprocess.run(["git", "--version"], capture_output=True).returncode == 0 else "NG", "git")
    try:
        import jsonschema  # noqa: F401
        line("OK", "python3 jsonschema")
    except ImportError:
        line("NG", "python3 jsonschema missing (apt install python3-jsonschema)")
    try:
        hardlinks = Path("/proc/sys/fs/protected_hardlinks").read_text().strip()
    except OSError:
        hardlinks = "unknown"
    line("OK" if hardlinks == "1" else "NG", f"fs.protected_hardlinks={hardlinks} (must be 1)")
    if wsl.is_wsl():
        holes = wsl.problems()
        line("NG" if holes else "OK", "WSL: Windows interop and drive mounts " +
             ("reachable: " + "; ".join(holes) if holes else "off"))
    else:
        line("OK", "not WSL")
    line("OK" if os.geteuid() != 0 else "NG", f"running as {current_user()} (not root)")

    cfgpath = Path(args.config).expanduser().resolve()
    print(f"config {cfgpath}")
    if not cfgpath.exists():
        line("NG", "no config: run init")
        return 0 if ok else 1
    try:
        cfg = load_cfg(cfgpath)
    except (ValueError, Refused, OSError) as exc:
        line("NG", str(exc))
        return 1
    for problem in local_policy(cfg):
        line("NG", problem)
    print(f"  publish={cfg['publish']} require_isolation={cfg['require_isolation']} workers={cfg['workers']} "
          f"models={cfg['model_concurrency']} task_globs={cfg['task_globs']} "
          f"allowed_task_ids={cfg.get('allowed_task_ids')}")
    verdict = stored_isolation(cfg)
    print("  last isolation verdict: " + ("none yet (start/sync/review run the probe)" if verdict is None else
          f"ok={verdict.get('ok')} reason={verdict.get('reason')}"))

    print(f"agent user {cfg.get('agent_user')}")
    if not cfg.get("agent_user"):
        return 1
    probe = subprocess.run(["sudo", "-n", "-u", cfg["agent_user"], "--", cfg["agent_python"],
                            str(ROOT / "scripts" / "agent-exec.py"), "--env-json", '{"PATH": "/usr/bin:/bin"}',
                            "--cwd", "/", "--timeout", "10", "--", "/usr/bin/id", "-un"],
                           capture_output=True, text=True, timeout=30)
    sudo_ok = probe.stdout.strip() == cfg["agent_user"]
    line("OK" if sudo_ok else "NG", "sudo rule: controller user -> agent user via scripts/agent-exec.py"
         + ("" if sudo_ok else f" ({probe.stderr.strip()[:200] or 'not working'})"))
    if sudo_ok:
        from controller.worker import stage_env
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "aiwos.py"), "auth"], capture_output=True,
                             text=True, env=stage_env(cfg, cfg["repo_dir"], 0), timeout=180)
        try:
            rows = json.loads(out.stdout)
        except ValueError:
            rows = []
            line("NG", "could not read the AI CLI login status")
        for row in rows:  # login state only: no model call, no credential printed
            line("OK" if row["usable"] else "WARN",
                 f"{row['agent']}: {'logged in' if row['usable'] else row['reason']}")
    return 0 if ok else 1


class Proc(NamedTuple):
    kind: str    # controller | worker | stage
    ticks: str   # start time from /proc: the identity a later process with the same PID cannot share


def _argv(pid):
    return [a.decode(errors="replace") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]


def _ppid(pid):
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _mine(pid):
    try:
        return os.stat(f"/proc/{pid}").st_uid == os.geteuid()
    except OSError:
        return False


def _cwd_arg_inside(argv, directory):
    try:
        value = argv[argv.index("--cwd") + 1]
    except (ValueError, IndexError):
        return False
    return inside(value, directory)


def config_processes(cfg):
    """This user's processes that belong to this config, other than the
    controller in the pid file: {pid: Proc}. A controller or worker must have
    this config in its command line and run from this checkout; a stage runner
    must carry a lease tag and point AIWOS_ROOT into this config's worktrees.
    The agent side (sudo and agent-exec.py working in this config's
    workspaces, owned by root or the agent user) is listed as 'agent': it is
    stopped only through its runner (sudo relaying the signal), never directly.
    Each process tree is listed once, by its top."""
    own, registered = os.getpid(), controller_pid(cfg)
    cfgarg = str(Path(cfg["_config_path"]).resolve())
    worktrees = Path(cfg["state_dir"]).resolve() / "worktrees"
    agent_exec = str(ROOT / "scripts" / "agent-exec.py")
    found = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) in (own, registered):
            continue
        pid = int(entry)
        try:
            ticks = process_start_ticks(pid)
            argv = _argv(pid)
            if agent_exec in argv and _cwd_arg_inside(argv, cfg["workspaces_dir"]):
                # sudo or agent-exec of this config's workspaces, whoever owns it: the
                # agent side of a stage. Waited for and reported, never signalled.
                if ticks and process_start_ticks(pid) == ticks:
                    found[pid] = Proc("agent", ticks)
                continue
            if not _mine(pid):
                continue
            kind = None
            for action, name in (("run", "controller"), ("worker", "worker")):
                expected = ["-m", "controller", action, "--config", cfgarg]
                if any(argv[i:i + 5] == expected for i in range(len(argv) - 4)) \
                        and os.readlink(f"/proc/{pid}/cwd") == str(ROOT):
                    kind = name
                    break
            if kind is None:
                env = dict(item.split(b"=", 1) for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                           if b"=" in item)
                root = env.get(b"AIWOS_ROOT", b"").decode(errors="replace")
                if b"AIWOS_LEASE_TAG" in env and root and inside(root, worktrees):
                    kind = "stage"
            if kind and ticks and process_start_ticks(pid) == ticks:  # still the process just read
                found[pid] = Proc(kind, ticks)
        except (OSError, ValueError):
            continue  # gone meanwhile
    return {pid: p for pid, p in found.items() if _ppid(pid) not in found}


def unsupervised(cfg):
    """Processes of this config that the controller in the pid file does not
    supervise: everything when it is not running (left behind by a crash: an
    AI may still be working), otherwise only a second, foreign controller."""
    procs = config_processes(cfg)
    if controller_pid(cfg):
        return {pid: p for pid, p in procs.items() if p.kind == "controller"}
    return procs


def describe(procs):
    return ", ".join(f"{p.kind} {pid}" for pid, p in sorted(procs.items()))


def open_verified(pid, ticks):
    """A pidfd for pid, only if it is still the process seen earlier (same
    start time, checked after the pidfd is open). A signal sent through it
    cannot reach a later process that reuses the PID. None: gone or reused."""
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return None
    except (AttributeError, OSError) as exc:  # no pidfd: a plain kill could hit a reused PID
        raise Refused(f"cannot signal processes safely without pidfd (Linux 5.3+, Python 3.9+): {exc}")
    if not ticks or process_start_ticks(pid) != ticks:
        os.close(fd)
        return None
    return fd


def send(fd, sig):
    try:
        signal.pidfd_send_signal(fd, sig)
        return True
    except ProcessLookupError:
        return False


class Pinned(NamedTuple):
    pid: int
    ticks: str
    fd: int       # pidfd, opened and checked against ticks while the parent was pinned
    mine: bool    # owned by this user: may be SIGKILLed
    relay: bool   # not ours but started by us (sudo, real uid = ours): SIGTERM asks it to relay


class Stop(NamedTuple):
    state: str        # gone | stopped | killed | running
    survivors: list   # [(pid, start ticks)] still running when stop gave up


def _real_uid(pid):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def pin_tree(pid, ticks):
    """Pidfds for pid and every descendant (any user), in breadth-first order.
    Taken before anything is signalled, while pid is itself pinned: each
    descendant is identified by its start time and must still be the child of
    an already pinned process after its pidfd is open. Only these processes are
    ever signalled or waited for; nothing is listed again later, so a PID that
    is reused after its owner exits cannot bring an unrelated tree in.
    None: pid is gone or is no longer the process seen."""
    top = open_verified(pid, ticks)
    if top is None:
        return None
    me = os.geteuid()
    order = [Pinned(pid, ticks, top, _mine(pid), False)]
    children = {}
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            parent = _ppid(int(entry))
            if parent is not None:
                children.setdefault(parent, []).append(int(entry))
    i = 0
    while i < len(order):
        parent = order[i].pid
        i += 1
        for child in children.get(parent, []):
            cticks = process_start_ticks(child)
            fd = open_verified(child, cticks) if cticks else None
            if fd is None:
                continue
            if _ppid(child) != parent:  # re-parented meanwhile: not provably part of this tree
                os.close(fd)
                continue
            mine = _mine(child)
            order.append(Pinned(child, cticks, fd, mine, not mine and _real_uid(child) == os.getuid() and me != 0))
    return order


def wait_all(pinned, seconds):
    """Wait until every pinned process has exited (its pidfd turns readable);
    return those still running."""
    left = {p.fd: p for p in pinned}
    deadline = time.time() + seconds
    while left:
        poller = select.poll()
        for fd in left:
            poller.register(fd, select.POLLIN)
        for fd, _ in poller.poll(max(0.0, deadline - time.time()) * 1000):
            left.pop(fd, None)
        if time.time() >= deadline:
            break
    return [p for p in pinned if p.fd in left]


def stop_process(pid, proc, first, grace, escalate):
    """Stop one process and everything below it, all pinned beforehand.

    `first` goes to the top only (a stage runner turns SIGINT into an orderly
    stop and has sudo relay it to the agent side). Success means every pinned
    process has exited, not just the top. With escalate, survivors that are
    ours get SIGKILL (deepest first) and a sudo we started gets SIGTERM so it
    relays; another user's process (the agent) is never signalled. Whatever
    still runs after that is returned as survivors and the result is
    'running': the caller must fail closed."""
    tree = pin_tree(pid, proc.ticks)
    if tree is None:
        return Stop("gone", [])
    try:
        send(tree[0].fd, first)
        left = wait_all(tree, grace)
        if not left:
            return Stop("stopped", [])
        if not escalate:
            return Stop("running", [(p.pid, p.ticks) for p in left])
        for p in reversed(left):  # deepest first: a child would be re-parented once its parent dies
            if p.mine:
                send(p.fd, signal.SIGKILL)
            elif p.relay:
                send(p.fd, signal.SIGTERM)
        left = wait_all(left, AGENT_STOP_S)
        return Stop("killed", []) if not left else Stop("running", [(p.pid, p.ticks) for p in left])
    finally:
        for p in tree:
            os.close(p.fd)


def still_running(pid, ticks):
    """The same process (start time) and not merely a zombie waiting to be reaped."""
    try:
        after = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return False
    return len(after) > 19 and after[19] == ticks and after[0] not in ("Z", "X")


def survivors_path(cfg):
    return Path(cfg["state_dir"]) / SURVIVORS_NAME


def live_survivors(cfg):
    """Processes an earlier stop could not end, that are still the same processes."""
    path = survivors_path(cfg)
    try:
        recorded = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    alive = [(pid, ticks) for pid, ticks in recorded if still_running(pid, ticks)]
    if not alive:
        path.unlink(missing_ok=True)
    return alive


def record_survivors(cfg, survivors):
    merged = sorted(set(map(tuple, live_survivors(cfg))) | set(map(tuple, survivors)))
    path = survivors_path(cfg)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def stop_leftovers(cfg):
    """Stop what a crash left running for this config, the way the controller
    stops a dead worker's stage, with every signal through a pidfd checked
    against the start time seen at detection. A controller gets SIGTERM and is
    waited for (never SIGKILL); a worker or a stage tree is stopped by
    stop_process; the agent side is never signalled, only waited for. Anything
    that cannot be shown to have ended is recorded: stop fails, and start,
    sync and review are refused until those processes are gone."""
    left, prior = unsupervised(cfg), live_survivors(cfg)
    if not left and not prior:
        return 0
    if left:
        print(f"WARNING: running without a controller: {describe(left)} (an AI stage may have been running)")
    survivors = list(prior)
    for pid, proc in left.items():
        if proc.kind == "controller":
            result = stop_process(pid, proc, signal.SIGTERM, cfg["kill_grace_s"] + 60, escalate=False)
            if result.state == "running":
                record_survivors(cfg, result.survivors)
                return die(f"controller {pid} not started by this CLI did not stop; left running", 1)
    for pid, proc in unsupervised(cfg).items():
        if proc.kind in ("worker", "stage"):
            first = signal.SIGINT if proc.kind == "stage" else signal.SIGTERM
            survivors += stop_process(pid, proc, first, cfg["kill_grace_s"], escalate=True).survivors
    # the agent side ends when its runner has had sudo relay the stop; give it the time agent-exec takes
    deadline = time.time() + cfg["kill_grace_s"] + AGENT_STOP_S
    while time.time() < deadline and unsupervised(cfg):
        time.sleep(0.5)
    survivors += [(pid, p.ticks) for pid, p in unsupervised(cfg).items()]
    survivors = sorted({(pid, ticks) for pid, ticks in survivors if still_running(pid, ticks)})
    if survivors:
        record_survivors(cfg, survivors)
        return die(f"could not show these stopped, left running: {survivors}. start, sync and review are refused "
                   f"until they end (an agent stage ends at its own timeout) or an administrator stops them", 1)
    survivors_path(cfg).unlink(missing_ok=True)
    print(f"Stopped leftover processes: {describe(left)}" if left else "Earlier leftover processes have ended")
    return 0


def refuse_if_busy(cfg, what):
    pid = controller_pid(cfg)
    if pid:
        raise Refused(f"{what} is refused while the controller runs (PID {pid}); stop it first")
    alive = live_survivors(cfg)
    if alive:
        raise Refused(f"{what} is refused: processes an earlier stop could not end are still running "
                      f"({alive}); they must end first")
    left = unsupervised(cfg)
    if left:
        raise Refused(f"{what} is refused: processes of this config are running without a controller "
                      f"({describe(left)}); run stop first")


def cmd_start(cfg):
    with single_operator(cfg):
        pid = controller_pid(cfg)
        if pid:
            print(f"Controller already running (PID {pid}); use status")
            return 0
        refuse_if_busy(cfg, "start")
        boundary_check(cfg)
        if run_controller(cfg, "check-config", quiet=True) != 0:
            return die("invalid config")
        pidpath, logpath, _ = paths(cfg)
        logfd = os.open(str(logpath), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(logfd, "ab") as logfile:
            proc = subprocess.Popen([sys.executable, "-m", "controller", "run", "--config", cfg["_config_path"]],
                                    cwd=ROOT, stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            time.sleep(2)
            if proc.poll() is not None:
                return die(f"controller exited with {proc.returncode}; see {logpath}", 1)
            ticks = process_start_ticks(proc.pid)
            if not ticks:
                raise Refused("cannot verify the controller's start time")
            tmp = pidpath.with_suffix(".tmp")
            tmp.write_text(json.dumps({"pid": proc.pid, "config": cfg["_config_path"], "start_ticks": ticks}) + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, pidpath)
        except BaseException:
            if proc.poll() is None:
                proc.terminate()  # the controller stops its own workers on SIGTERM
            raise
        print(f"Controller started (PID {proc.pid}): workers={cfg['workers']} models={cfg['model_concurrency']} "
              f"publish={cfg['publish']}")
        print(f"Log: {logpath}")
        return 0


def cmd_status(cfg, raw=False):
    pid = controller_pid(cfg)
    print(f"Local controller: {'RUNNING (PID ' + str(pid) + ')' if pid else 'STOPPED'}")
    left = unsupervised(cfg)
    if left:
        print(f"WARNING: running without a controller: {describe(left)}; an AI stage may still be running. "
              f"Run stop to end them.")
    alive = live_survivors(cfg)
    if alive:
        print(f"WARNING: an earlier stop could not end {alive}; start, sync and review are refused until they end")
    verdict = stored_isolation(cfg)
    if verdict is not None:
        print(f"Isolation: ok={verdict.get('ok')} reason={verdict.get('reason')}")
    if not (Path(cfg["state_dir"]) / "aiwos.db").exists():
        print("No task database yet")
        return 0
    return run_controller(cfg, "status", ("--json",) if raw else ())


def cmd_stop(cfg):
    with single_operator(cfg):
        pidpath = paths(cfg)[0]
        pid = controller_pid(cfg)
        if pid is None:
            print("Controller not running; no controller was signalled")
        else:
            # the controller stops its workers and their stage processes itself; never SIGKILL it
            # the start time recorded at start (and just verified), not one read now:
            # a PID reused in between must not pass the check
            recorded = json.loads(pidpath.read_text())["start_ticks"]
            result = stop_process(pid, Proc("controller", recorded), signal.SIGTERM,
                                  cfg["kill_grace_s"] + 60, escalate=False)
            if result.state == "running":
                record_survivors(cfg, result.survivors)
                return die("stop timed out; the controller was left running for inspection", 1)
            print("Controller stopped")
        pidpath.unlink(missing_ok=True)
        # a worker killed outright leaves its stage (and the AI under it) running
        # even after a clean controller shutdown, until the stage's own timeout
        return stop_leftovers(cfg)


def cmd_sync(cfg):
    # the running controller fetches and updates its worktrees itself (every
    # sync_interval_s); a second git writer on the same clone would race it
    with single_operator(cfg):
        refuse_if_busy(cfg, "sync")
        boundary_check(cfg)  # sync checks the AI CLI logins as the agent user
        return run_controller(cfg, "sync-once")


def cmd_review(cfg, task):
    with single_operator(cfg):
        repo = Path(cfg["repo_dir"]).resolve()
        source = (Path(task) if Path(task).is_absolute() else repo / task).resolve()
        if not source.is_relative_to(repo) or not source.is_file() or source.suffix != ".json":
            return die("task must be an existing JSON contract inside repo_dir")
        rel = source.relative_to(repo).as_posix()
        if not any(fnmatch.fnmatchcase(rel, g) for g in cfg["task_globs"]):
            return die(f"{rel} is not one of this queue's tasks (task_globs {cfg['task_globs']})")
        try:
            task_id = json.loads(source.read_text(encoding="utf-8")).get("task_id")
        except (ValueError, AttributeError):
            return die(f"{rel} is not a JSON contract")
        from controller.config import task_allowed
        if task_id != SMOKE_TASK_ID or not task_allowed(cfg, task_id):
            return die(f"{rel} has task_id {str(task_id)[:60]!r}: only {SMOKE_TASK_ID} may run in the smoke test")
        refuse_if_busy(cfg, "review")  # two writers on one task
        boundary_check(cfg)
        from controller.worker import stage_env
        env = {k: v for k, v in stage_env(cfg, repo, 0).items() if k not in TOKEN_ENV}
        # the runner enforces the assigned reviewer, the run evidence and --execute
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "aiwos.py"), "run", str(source),
                               "--role", "reviewer", "--execute"], cwd=ROOT, env=env, check=False).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aiwos-local", description="AI WORK OS local controller CLI")
    parser.add_argument("--config", default=str(CONFIG_DEFAULT), help="controller config JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="write a local config (publish=none, require_isolation)")
    init.add_argument("--repo-dir", required=True)
    init.add_argument("--state-dir", required=True)
    init.add_argument("--workspaces-dir", required=True)
    init.add_argument("--agent-user", required=True)
    init.add_argument("--agent-group", required=True)
    init.add_argument("--credential-file", action="append", default=[])
    init.add_argument("--github-repo", default="masube-arent/ai-work-os")
    sub.add_parser("doctor", help="check the machine, config, isolation and logins; starts no model")
    sub.add_parser("start", help="start the controller in the background (after the isolation probe)")
    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    sub.add_parser("stop", help="SIGTERM the verified controller and wait")
    sub.add_parser("sync", help="fetch and enqueue once, without workers")
    rv = sub.add_parser("review", help="run a contract's assigned reviewer (controller stopped)")
    rv.add_argument("task", help="task JSON path inside repo_dir")
    args = parser.parse_args(argv)
    try:
        ensure_linux()
        verified_layout()
        refuse_root()
        if args.command == "init":
            return cmd_init(args)
        if args.command == "doctor":
            return cmd_doctor(args)
        cfg = load_cfg(args.config)
        if args.command == "start":
            return cmd_start(cfg)
        if args.command == "stop":
            return cmd_stop(cfg)
        if args.command == "status":
            return cmd_status(cfg, args.json)
        if args.command == "sync":
            return cmd_sync(cfg)
        return cmd_review(cfg, args.task)
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError) as exc:
        return die(f"{type(exc).__name__}: {exc}", 1)


if __name__ == "__main__":
    sys.exit(main())
