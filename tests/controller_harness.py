"""Sandbox for controller tests: a bare git remote standing in for GitHub,
synthetic task contracts, and one stub executable that plays both `claude`
and `codex` without calling any model.

The stub reads its behaviour from <stub>.json at every call, so a test can
change it while the controller runs, and appends one JSON line per model
invocation (start/end time, pid, role) to the spy file.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STUB = r'''#!/usr/bin/env python3
import json, os, re, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.basename(__file__).split("-")[-1]
cfg = json.load(open(os.path.join(HERE, "stub.json")))
args = sys.argv[1:]
if args == ["--version"]:
    print("0.0.0 (stub " + CLI + ")"); sys.exit(0)
logged_in = cfg.get(CLI + "_logged_in", True)
if CLI == "claude" and args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": logged_in, "authMethod": "oauth_token", "apiProvider": "firstParty"})); sys.exit(0)
if CLI == "codex" and args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT" if logged_in else "Not logged in"); sys.exit(0 if logged_in else 1)
prompt = sys.stdin.read()
reviewer = ("--json-schema" in args) or ("read-only" in args)
role = "reviewer" if reviewer else "worker"
start = time.time()
time.sleep(cfg.get(role + "_sleep", 0.3))

def spy(extra=None):
    line = json.dumps({"cli": CLI, "role": role, "start": start, "end": time.time(), "pid": os.getpid(),
                       "cwd": os.getcwd(), **(extra or {})}) + "\n"
    fd = os.open(cfg["spy"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    os.write(fd, line.encode()); os.close(fd)

if not reviewer and cfg.get("rate_limit_" + CLI, 0) > 0:
    cfg["rate_limit_" + CLI] -= 1
    json.dump(cfg, open(os.path.join(HERE, "stub.json"), "w"))
    spy({"rate_limited": True})
    print(json.dumps({"type": "result", "is_error": True, "session_id": "s-%d" % os.getpid(),
                      "result": "Claude AI usage limit reached. Try again later.", "num_turns": 1}))
    sys.exit(1)
if reviewer:
    ids = re.findall(r"\*\*(AC\d+)\*\*", prompt)
    review = {"criteria": [{"criterion_id": i, "verdict": "PASS", "reason": "stub"} for i in ids],
              "defects": [], "summary": "stub review"}
    spy()
    if CLI == "codex":
        open(args[args.index("-o") + 1], "w").write(json.dumps(review))
        print(json.dumps({"type": "thread.started", "thread_id": "codex-%d" % os.getpid()}))
    else:
        print(json.dumps({"type": "result", "is_error": False, "session_id": "claude-r-%d" % os.getpid(),
                          "num_turns": 1, "result": "", "structured_output": review,
                          "modelUsage": {"stub-model": {"inputTokens": 1, "outputTokens": 1}}}))
    sys.exit(0)
path = re.search(r"変更してよいパス: (\S+)", prompt).group(1).rstrip(",")
os.makedirs(path, exist_ok=True)
open(os.path.join(path, "out.txt"), "w").write("done\n")
spy()
if CLI == "codex":
    print(json.dumps({"type": "thread.started", "thread_id": "codex-w-%d" % os.getpid()}))
else:
    print(json.dumps({"type": "result", "is_error": False, "session_id": "claude-w-%d" % os.getpid(),
                      "num_turns": 2, "result": "done", "modelUsage": {"stub-model": {"inputTokens": 5, "outputTokens": 5}}}))
'''


def contract(task_id, worker="claude-code", reviewer="codex-cli"):
    d = f"tasks/{task_id}"
    return {
        "schema_version": "0.2", "task_id": task_id, "task_type": "development",
        "objective": f"Synthetic controller test task {task_id}: write {d}/out.txt containing 'done'.",
        "inputs": [], "constraints": ["test fixture"], "allowed_paths": [d],
        "acceptance_criteria": [
            {"id": "AC1", "description": "out.txt says done", "dimension": "spec_conformance",
             "verification": {"method": "command", "command": f"grep -q done {d}/out.txt"}},
            {"id": "AC2", "description": "out.txt is not empty", "dimension": "correctness",
             "verification": {"method": "command", "command": f"test -s {d}/out.txt"}},
            {"id": "AC3", "description": "nothing else needed", "dimension": "completeness",
             "verification": {"method": "inspection"}},
        ],
        "failure_conditions": [{"id": "FC1", "description": "file missing", "on_trigger": "rework"}],
        "assigned_agent": worker, "reviewer": reviewer, "status": "ready", "evidence": [],
        "quality": {k: {"verdict": "UNVERIFIED", "evidence_ids": [], "judged_by": None}
                    for k in ("spec_conformance", "correctness", "completeness")},
        "retry_count": 0, "max_retries": 2, "escalation_reason": None,
        "history": [{"from": "draft", "to": "ready", "at": "2026-10-11T00:00:00Z", "by": "claude-code",
                     "note": "test fixture"}],
    }


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Sandbox:
    def __init__(self, n_tasks=3, workers=10, model_concurrency=2, stub=None, config=None, task_kwargs=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiwos-ctl-"))
        self.remote = self.tmp / "remote.git"
        seed = self.tmp / "seed"
        shutil.copytree(ROOT, seed, ignore=shutil.ignore_patterns(".git", "runs", "__pycache__", ".test-evidence"))
        self.task_ids = [f"T-{i:04d}" for i in range(1, n_tasks + 1)]
        for tid in self.task_ids:
            (seed / "tasks" / tid).mkdir(parents=True)
            (seed / "tasks" / tid / "task.json").write_text(
                json.dumps(contract(tid, **(task_kwargs or {})), ensure_ascii=False, indent=2))
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        git(seed, "init", "-q", "-b", "main")
        git(seed, "add", "-A")
        git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
        git(seed, "remote", "add", "origin", str(self.remote))
        git(seed, "push", "-q", "origin", "main")
        self.seed = seed
        self.repo = self.tmp / "repo"
        git(self.tmp, "clone", "-q", str(self.remote), str(self.repo))
        git(self.repo, "config", "user.name", "aiwos-controller")
        git(self.repo, "config", "user.email", "aiwos-controller@localhost")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for cli in ("claude", "codex"):
            p = self.bin / f"stub-{cli}"
            p.write_text(STUB)
            p.chmod(0o755)
        self.spy = self.tmp / "spy.jsonl"
        self.set_stub({"spy": str(self.spy), **(stub or {})})
        self.state = self.tmp / "state"
        cfg = {
            "repo_dir": str(self.repo), "state_dir": str(self.state), "code_dir": str(ROOT),
            "task_globs": ["tasks/*/task.json"], "workers": workers, "model_concurrency": model_concurrency,
            "poll_interval_s": 0.3, "sync_interval_s": 2, "lease_ttl_s": 8, "heartbeat_s": 1,
            "stage_timeout_s": {"work": 60, "check": 60, "review": 60, "judge": 60, "resume": 30},
            "kill_grace_s": 3, "publish": "git", "auth_recheck_s": 2, "agent_status_ttl_s": 600,
            "backoff_initial_s": 3, "backoff_max_s": 10,
            "agent_bins": {"claude": str(self.bin / "stub-claude"), "codex": str(self.bin / "stub-codex")},
        }
        cfg.update(config or {})
        self.config = self.tmp / "controller.json"
        self.config.write_text(json.dumps(cfg, indent=2))
        self.proc = None

    def set_stub(self, values):
        path = self.bin / "stub.json"
        current = json.loads(path.read_text()) if path.exists() else {}
        current.update(values)
        path.write_text(json.dumps(current))

    def env(self):
        # stub tests carry no GitHub credential, so the execution gate (Security
        # Fix 002 item 4) lets them run without OS-level isolation. Tests that
        # need a credential present set it themselves.
        env = dict(os.environ)
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "AIWOS_ROOT"):
            env.pop(name, None)
        return env

    def start(self):
        logf = open(self.tmp / "controller.log", "ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "controller", "run", "--config", str(self.config)],
                                     cwd=ROOT, env=self.env(), stdout=logf, stderr=subprocess.STDOUT)
        logf.close()
        return self.proc

    def stop(self, timeout=60):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "controller", *args, "--config", str(self.config)],
                              cwd=ROOT, env=self.env(), capture_output=True, text=True, timeout=120)

    @contextmanager
    def db(self):
        con = sqlite3.connect(self.state / "aiwos.db", timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
        finally:
            con.close()

    def tasks(self):
        with self.db() as con:
            return {r["task_id"]: dict(r) for r in con.execute("SELECT * FROM tasks")}

    def events(self, kind=None):
        with self.db() as con:
            sql, args = "SELECT * FROM events", ()
            if kind:
                sql, args = sql + " WHERE kind=?", (kind,)
            return [dict(r) for r in con.execute(sql + " ORDER BY id", args)]

    def workers(self):
        with self.db() as con:
            return [dict(r) for r in con.execute("SELECT * FROM workers ORDER BY worker_id")]

    def wait_for(self, predicate, timeout=180, interval=0.5):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if predicate():
                    return True
            except sqlite3.OperationalError:
                pass
            time.sleep(interval)
        return False

    def spy_calls(self):
        if not self.spy.exists():
            return []
        return [json.loads(line) for line in self.spy.read_text().splitlines() if line.strip()]

    def remote_contract(self, task_id):
        ref = f"aiwos/task/{task_id}"
        out = subprocess.run(["git", "--git-dir", str(self.remote), "show", f"{ref}:tasks/{task_id}/task.json"],
                             capture_output=True, text=True)
        return json.loads(out.stdout) if out.returncode == 0 else None

    def close(self):
        self.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


def max_overlap(calls):
    """Largest number of model invocations running at the same moment."""
    points = sorted([(c["start"], 1) for c in calls] + [(c["end"], -1) for c in calls], key=lambda p: (p[0], p[1]))
    cur = best = 0
    for _, delta in points:
        cur += delta
        best = max(best, cur)
    return best
