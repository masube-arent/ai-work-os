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
publish=none, require_isolation=true, 10 workers, 1 model slot and contracts
under tasks/; start, stop, sync and review take one lock per state directory. Based on the audited
aiwos-local-cli.zip (docs/local-cli.md lists what was changed and why).
"""
import argparse
import fcntl
import fnmatch
import json
import os
import pwd
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONFIG_DEFAULT = Path(os.environ.get("AIWOS_CONFIG", "~/.config/aiwos/local.json")).expanduser()
# kept in the state directory, under names no config can take (see config_location_problem)
PID_NAME, LOG_NAME, LOCK_NAME = "local-controller.pid", "local-controller.log", "local-controller.lock"
TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")
# the first local runs: these values exactly, and only contracts under tasks/
LOCAL_WORKERS, LOCAL_MODELS = 10, 1
LOCAL_TASK_GLOBS = ["tasks/*.json", "tasks/*/*.json"]


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


def task_glob_ok(glob):
    """A queue pattern that can only match contracts under tasks/."""
    return (isinstance(glob, str) and glob.startswith("tasks/") and len(glob) > len("tasks/")
            and "\\" not in glob and ".." not in glob.split("/") and not glob.endswith("/"))


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
    globs = cfg.get("task_globs")
    if not isinstance(globs, list) or not globs or not all(task_glob_ok(g) for g in globs):
        problems.append(f"task_globs={globs!r}: only patterns under tasks/ are allowed")
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
               task_globs=list(LOCAL_TASK_GLOBS),
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
    print(f"Config created: {cfgpath} (publish=none, require_isolation=true, workers=10, models=1)")
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
          f"models={cfg['model_concurrency']} task_globs={cfg['task_globs']}")
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


def _argv(pid):
    return [a.decode(errors="replace") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]


def _ppid(pid):
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def config_processes(cfg):
    """This user's processes that belong to this config, other than the
    controller in the pid file: {pid: kind}. kind is 'controller' (one not
    started by this CLI), 'worker', or 'stage' (a stage runner, found by the
    lease tag in its environment; the agent side below it is reached through
    it, sudo relaying the signal). Each process tree is listed once, by its top."""
    me, own, registered = os.geteuid(), os.getpid(), controller_pid(cfg)
    cfgarg = str(Path(cfg["_config_path"]).resolve())
    worktrees = Path(cfg["state_dir"]).resolve() / "worktrees"
    found = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) in (own, registered):
            continue
        pid = int(entry)
        try:
            if os.stat(f"/proc/{pid}").st_uid != me:
                continue
            argv = _argv(pid)
            for action, kind in (("run", "controller"), ("worker", "worker")):
                expected = ["-m", "controller", action, "--config", cfgarg]
                if any(argv[i:i + 5] == expected for i in range(len(argv) - 4)):
                    found[pid] = kind
                    break
            else:
                env = dict(item.split(b"=", 1) for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                           if b"=" in item)
                root = env.get(b"AIWOS_ROOT", b"").decode(errors="replace")
                if b"AIWOS_LEASE_TAG" in env and root and inside(root, worktrees):
                    found[pid] = "stage"
        except (OSError, ValueError):
            continue  # gone meanwhile
    return {pid: kind for pid, kind in found.items() if _ppid(pid) not in found}


def unsupervised(cfg):
    """Processes of this config that the controller in the pid file does not
    supervise: everything when it is not running (left behind by a crash: an
    AI may still be working), otherwise only a second, foreign controller."""
    procs = config_processes(cfg)
    if controller_pid(cfg):
        return {pid: kind for pid, kind in procs.items() if kind == "controller"}
    return procs


def describe(procs):
    return ", ".join(f"{kind} {pid}" for pid, kind in sorted(procs.items()))


def wait_gone(pids, seconds):
    from controller.store import pid_alive
    deadline = time.time() + seconds
    while time.time() < deadline and any(pid_alive(p) for p in pids):
        time.sleep(0.5)
    return [p for p in pids if pid_alive(p)]


def stop_leftovers(cfg):
    """Stop what a crash left running for this config, the way the controller
    stops a dead worker's stage: a controller gets SIGTERM and is waited for
    (never SIGKILL); a worker or a stage tree gets SIGTERM/SIGINT, then SIGKILL
    after kill_grace_s. Their leased tasks return to the queue when the next
    controller finds the lease expired."""
    left = unsupervised(cfg)
    if not left:
        return 0
    print(f"WARNING: running without a controller: {describe(left)} (an AI stage may have been running)")
    controllers = [pid for pid, kind in left.items() if kind == "controller"]
    for pid in controllers:
        os.kill(pid, signal.SIGTERM)
    if wait_gone(controllers, cfg["kill_grace_s"] + 60):
        return die(f"controller not started by this CLI did not stop: {controllers}; left running", 1)
    from controller.procs import kill_tree
    for pid, kind in unsupervised(cfg).items():
        kill_tree(pid, grace=cfg["kill_grace_s"], first=signal.SIGINT if kind == "stage" else signal.SIGTERM)
    remaining = unsupervised(cfg)
    if remaining:
        return die(f"still running, left for inspection: {describe(remaining)}", 1)
    print(f"Stopped leftover processes: {describe(left)}")
    return 0


def refuse_if_busy(cfg, what):
    pid = controller_pid(cfg)
    if pid:
        raise Refused(f"{what} is refused while the controller runs (PID {pid}); stop it first")
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
            os.kill(pid, signal.SIGTERM)
            # the controller stops its workers and their stage processes itself; never SIGKILL it
            if wait_gone([pid], cfg["kill_grace_s"] + 60) and controller_pid(cfg):
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
