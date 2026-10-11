#!/usr/bin/env python3
"""AI WORK OS runner (Phase 2): run agents against a task contract and record
what actually happened.

  aiwos.py auth                                   which agent CLIs can run here
  aiwos.py run TASK --role worker   [--execute]   implement (dry run unless --execute)
  aiwos.py check TASK               [--execute]   run the command criteria on the worker's commit
  aiwos.py run TASK --role reviewer [--execute]   independent review of that commit
  aiwos.py judge TASK               [--write]     quality verdicts + state transition
  aiwos.py transition TASK --to S --by A [--reason R]
  aiwos.py status TASK
  aiwos.py metrics [--json]

Design rules (core/execution.md):
- Dry run by default. Nothing starts an agent without --execute, and every
  agent run has a wall-clock timeout. There is no loop: one command, one run.
- The contract decides who runs. There is no --agent override; swapping roles
  means a different contract.
- Each run gets its own workspace exported from a fixed commit, without runs/,
  and the worker's change becomes its own branch run/<task>/<run_id>.
- A secret in any output, a change outside allowed_paths, or a reviewer that
  modifies what it reviews stops the run and escalates the task.
- Every task update is validated and written only if the file still has the
  hash it had when it was read; a second writer gets a conflict, not a merge.
- Values a CLI did not report are null.

Exit codes: 0 ok, 1 run finished but not completed, 2 invalid contract or
usage, 3 blocked (CLI missing, not authenticated, precondition), 4 conflict,
5 security or scope violation.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE_ROOT = HERE.parent  # rules and schemas: always this checkout
# data: contracts, runs/ and git. AIWOS_ROOT lets the controller run this
# trusted copy against a task worktree (see validate-task.py).
ROOT = Path(os.environ["AIWOS_ROOT"]).resolve() if os.environ.get("AIWOS_ROOT") else CODE_ROOT
sys.path.insert(0, str(HERE))
from adapters import ADAPTERS, scrubbed_env  # noqa: E402
from scan_secrets import find_in_text  # noqa: E402
import isolation  # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vt = _load("validate_task", "validate-task.py")
rb = _load("render_brief", "render-brief.py")

RUNS = ROOT / "runs"
REVIEW_SCHEMA = CODE_ROOT / "contracts" / "review.schema.json"
RUN_SCHEMA = CODE_ROOT / "contracts" / "run.schema.json"
OUTPUT_CAP = 2_000_000
DEFAULT_TIMEOUT = {"worker": 900, "reviewer": 600, "check": 300}
BLOCKING_SEVERITIES = {"blocker", "high"}
EXIT_OK, EXIT_NOT_COMPLETED, EXIT_INVALID, EXIT_BLOCKED, EXIT_CONFLICT, EXIT_SECURITY = 0, 1, 2, 3, 4, 5


class Conflict(Exception):
    pass


class Refused(Exception):
    """A precondition failed; nothing was executed."""


# --------------------------------------------------------------------------- helpers

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def rel(path):
    return Path(path).resolve().relative_to(ROOT).as_posix()


def git(*args, cwd=ROOT, env=None, data=None):
    out = subprocess.run(["git", *args], cwd=cwd, env=env, input=data, capture_output=True, check=True)
    return out.stdout.decode().strip()


def validator():
    return vt.load_schema_validator()


# --------------------------------------------------------------------------- task file

def read_task(path):
    data = Path(path).read_bytes()
    return json.loads(data), sha256(data)


def write_task(path, task, expected_sha):
    """Write task only if it still has expected_sha and the result is valid."""
    path = Path(path)
    lock = path.with_name(path.name + ".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise Conflict(f"{rel(lock)} exists: another process is updating this task")
    try:
        current = sha256(path.read_bytes())
        if current != expected_sha:
            raise Conflict(f"{rel(path)} changed since it was read (expected {expected_sha[:12]}, "
                           f"found {current[:12]}); not overwriting")
        errors = vt.validate_task(task, validator())
        if errors:
            raise Refused("refusing to write an invalid contract:\n  - " + "\n  - ".join(errors))
        data = (json.dumps(task, ensure_ascii=False, indent=2) + "\n").encode()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return sha256(data)
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


def transition(task, to, note):
    task["history"].append({"from": task["status"], "to": to, "at": now(), "by": "aiwos-runner", "note": note})
    task["status"] = to


def escalate(task, reason):
    transition(task, "escalated", reason)
    task["escalation_reason"] = reason


@contextmanager
def active_lock(task_id, run_id):
    """One run per task at a time."""
    directory = RUNS / task_id
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".active"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise Conflict(f"another run is active on {task_id}: {lock.read_text().strip()} "
                       f"(delete {rel(lock)} only if that process is gone)")
    os.write(fd, f"{run_id} pid={os.getpid()} since={now()}\n".encode())
    os.close(fd)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


# --------------------------------------------------------------------------- runs

def run_records(task):
    """[(evidence, record)] for run_record evidence, oldest first."""
    out = []
    for ev in task["evidence"]:
        if ev["kind"] == "run_record":
            out.append((ev, json.loads((ROOT / ev["path"]).read_text(encoding="utf-8"))))
    return out


def latest(task, role, status="completed", target=None):
    found = [r for _, r in run_records(task) if r["role"] == role and r["status"] == status
             and (target is None or r["target_commit"] == target)]
    return found[-1] if found else None


def evidence_for_run(task, run):
    prefix = f"runs/{run['task_id']}/{run['run_id']}/"
    return [ev for ev in task["evidence"] if ev.get("path", "").startswith(prefix)]


def new_record(task, task_path, task_sha, run_id, role, agent, cli, timeout):
    return {
        "run_schema_version": "0.2", "run_id": run_id, "task_id": task["task_id"],
        "task_path": rel(task_path), "task_type": task["task_type"], "role": role, "agent": agent,
        "status": "blocked", "stop_reason": None, "cli": cli,
        "auth": {"checked": False, "logged_in": None, "method": None, "provider": None},
        "argv": [], "model_requested": None, "models_reported": None, "session_id_reported": None,
        "cost_usd_reported": None, "num_turns_reported": None, "base_commit": None,
        "target_commit": None, "result_commit": None, "branch": None,
        "task_sha256_at_start": task_sha, "retry_count_at_start": task["retry_count"],
        "started_at": now(), "ended_at": None, "duration_s": None, "exit_code": None,
        "timeout_s": timeout, "changed_files": None, "files": {},
        "security": {"secret_hits": [], "out_of_scope_paths": [], "workspace_modified_by_reviewer": None},
    }


def store(run_dir, record, name, data):
    """Write one run file (capped) and register its hash."""
    if len(data) > OUTPUT_CAP:
        data = data[:OUTPUT_CAP] + f"\n[aiwos: truncated at {OUTPUT_CAP} bytes]\n".encode()
    target = run_dir / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    record["files"][name] = sha256(data)


def scan(record, source, data):
    hits = [{"source": source, "line": line, "pattern": name}
            for line, name in find_in_text(data.decode("utf-8", errors="replace"))]
    record["security"]["secret_hits"].extend(hits)
    return bool(hits)


def finish(run_dir, record):
    """Write run.json last; return its sha256. A malformed record is a runner bug."""
    from jsonschema import Draft202012Validator
    record["ended_at"] = record["ended_at"] or now()
    problems = [e.message for e in Draft202012Validator(json.loads(RUN_SCHEMA.read_text())).iter_errors(record)]
    if problems:
        raise RuntimeError(f"run record does not match contracts/run.schema.json: {problems}")
    data = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode()
    (run_dir / "run.json").write_bytes(data)
    return sha256(data)


def add_evidence(task, ev_id, kind, path, digest, produced_by, criterion_ids=(), **extra):
    ev = {"id": ev_id, "kind": kind, "path": rel(path), "sha256": digest,
          "produced_by": produced_by, "created_at": now(), "criterion_ids": list(criterion_ids)}
    ev.update({k: v for k, v in extra.items() if v is not None})
    task["evidence"].append(ev)
    return ev_id


# --------------------------------------------------------------------------- workspace & process

def workspace_root():
    return Path(os.environ.get("AIWOS_WORKSPACES", Path.home() / ".cache" / "aiwos" / "workspaces"))


class Workspace:
    """One run's workspace, split so the agent can reach only what it needs:
      tree/  the files of `commit` without runs/ (all the agent sees; writable
             for a worker or a check, read-only for a reviewer)
      out/   where an agent CLI may write its own output files
      git/   git metadata for tree/, owned by the trusted side only. Keeping it
             out of tree/ means an agent cannot plant git config or hooks
             (core.fsmonitor, hooks) that the trusted side would then run.
    It shares no objects with this repository, so other runs' branches are not
    reachable through git."""

    SAFE = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "core.untrackedCache=false"]

    def __init__(self, commit, run_id, writable):
        root = workspace_root()
        root.mkdir(parents=True, exist_ok=True)
        isolation.traversable(root)
        self.root = root / run_id
        self.root.mkdir()
        self.tree, self.out, self.gitdir = self.root / "tree", self.root / "out", self.root / "git"
        for d in (self.tree, self.out, self.gitdir):
            d.mkdir()
        os.chmod(self.gitdir, 0o700)
        tar = subprocess.run(["git", "archive", "--format=tar", commit, "--", ".", ":(exclude)runs"],
                             cwd=ROOT, capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(self.tree)], input=tar, check=True)
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("-c", "user.name=aiwos-runner", "-c", "user.email=aiwos-runner@localhost",
                 "commit", "-q", "-m", f"aiwos base {commit}")
        self.base = self.git("rev-parse", "HEAD")
        isolation.traversable(self.root)
        isolation.share(self.tree, writable)
        isolation.share(self.out, True)

    def git(self, *args, raw=False):
        out = subprocess.run(["git", *self.SAFE, f"--git-dir={self.gitdir}", f"--work-tree={self.tree}", *args],
                             cwd=self.tree, capture_output=True, check=True,
                             env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        return out.stdout if raw else out.stdout.decode().strip()


def make_workspace(commit, run_id, writable=True):
    ws = Workspace(commit, run_id, writable)
    return ws, ws.base


def execute(argv, cwd, env, stdin_text, timeout):
    """Run an untrusted command (an agent CLI or an acceptance command): as the
    agent user when isolation is configured. The agent-side launcher gets a
    slightly longer deadline so this one fires first and is recorded."""
    start = time.monotonic()
    argv, env, cwd = isolation.wrap(argv, env, cwd, timeout + 30)
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    state = "exited"
    try:
        out, err = proc.communicate(stdin_text.encode() if stdin_text is not None else None, timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        state = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "interrupted"
        _killpg(proc.pid, signal.SIGTERM)  # under isolation sudo relays this to the agent side
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            _killpg(proc.pid, signal.SIGKILL)  # the agent-side launcher dies with its parent
            out, err = proc.communicate()
    return state, proc.returncode, out or b"", err or b"", round(time.monotonic() - start, 1)


def _killpg(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def commit_patch(base, patch, branch, message):
    """Apply patch onto base as a new commit on `branch`, without touching the
    working tree or the current branch."""
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(d) / "index"),
                   GIT_AUTHOR_NAME="aiwos-runner", GIT_AUTHOR_EMAIL="aiwos-runner@localhost",
                   GIT_COMMITTER_NAME="aiwos-runner", GIT_COMMITTER_EMAIL="aiwos-runner@localhost")
        git("read-tree", base, env=env)
        git("apply", "--cached", "--binary", "-", env=env, data=patch)
        tree = git("write-tree", env=env)
        commit = git("commit-tree", tree, "-p", base, "-m", message, env=env)
    git("branch", branch, commit)
    return commit


def out_of_scope(task, files):
    allowed = [p.rstrip("/") for p in task.get("allowed_paths", [])]
    bad = []
    for f in files:
        inside = any(f == p or f.startswith(p + "/") for p in allowed) if allowed else True
        if f.startswith("runs/") or not inside:
            bad.append(f)
    return bad


# --------------------------------------------------------------------------- prompts

WORKER_RULES = """
## 実行環境の規則（AI WORK OS runner）
- この作業ディレクトリはこのタスク専用のコピーです。ここ以外のファイルを読まない・変更しない。
- git commit / git push をしない。ファイルを編集するだけでよい（runner が差分を記録しコミットする）。
- 変更してよいパス: {allowed}
- 受入基準の検証コマンドは runner が別途実行し、その結果が判定に使われる。あなたの完了報告は合格の根拠にならない。
- 最後に、何をどう変えたかを3行以内で報告する。
"""

REVIEWER_RULES = """
## レビュー対象
- 対象コミット: `{commit}`（作業ディレクトリはこのコミットの内容。ファイルを変更しない）
- 実装者: {worker}（実行記録 {worker_run}）

## 変更差分
```diff
{patch}
```

## runner による機械的検証の結果（事実。実装者の申告ではない）
{checks}

## 出力
- すべての受入基準について criterion_id・verdict（PASS/FAIL/UNVERIFIED）・reason を返す。
- 検証できない基準は UNVERIFIED とする。推測で PASS にしない。
- 欠陥は severity（blocker/high/medium/low）・dimension・file・description で列挙する。なければ空配列。
- 出力は指定の JSON スキーマに従う。
"""


def worker_prompt(task, last_review):
    text = rb.render(task, "worker")
    text += WORKER_RULES.format(allowed=", ".join(task.get("allowed_paths", [])) or "（指定なし）")
    if last_review:
        text += "\n## 前回レビューの指摘（修正対象）\n"
        for d in last_review.get("defects", []):
            text += f"- [{d['severity']}] {d['file']}: {d['description']}\n"
        for c in last_review.get("criteria", []):
            if c["verdict"] == "FAIL":
                text += f"- {c['criterion_id']} FAIL: {c['reason']}\n"
    return text


def reviewer_prompt(task, worker_run, patch, checks):
    if checks:
        rows = ["| 基準 | コマンド | 終了コード | 期待 | 結果 |", "|---|---|---|---|---|"]
        rows += [f"| {c['criterion_id']} | `{c['command']}` | {c['exit_code']} | {c['expected_exit_code']} | "
                 f"{'PASS' if c['passed'] else 'FAIL'} |" for c in checks]
        check_text = "\n".join(rows)
    else:
        check_text = "（コマンドで検証する受入基準なし）"
    return rb.render(task, "reviewer") + REVIEWER_RULES.format(
        commit=worker_run["result_commit"], worker=worker_run["agent"], worker_run=worker_run["run_id"],
        patch=patch.decode("utf-8", errors="replace"), checks=check_text)


# --------------------------------------------------------------------------- commands

def load_valid(task_arg):
    path = Path(task_arg).resolve()
    task, digest = read_task(path)
    errors = vt.validate_task(task, validator())
    if errors:
        raise Refused(f"{task_arg} is not a valid contract:\n  - " + "\n  - ".join(errors))
    return path, task, digest


def cli_record(adapter):
    path = adapter.locate()
    return path, {"name": adapter.cli, "path": path, "version": adapter.version(path) if path else None}


def cmd_auth(args):
    rows = []
    for agent, adapter in ADAPTERS.items():
        path, cli = cli_record(adapter)
        if not path:
            rows.append({"agent": agent, "cli": cli, "auth": None, "usable": False, "reason": "CLI not found"})
            continue
        auth, refusal = adapter.auth(path)
        rows.append({"agent": agent, "cli": cli, "auth": auth, "usable": refusal is None, "reason": refusal})
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return EXIT_OK if all(r["usable"] for r in rows) else EXIT_BLOCKED


def cmd_run(args):
    path, task, task_sha = load_valid(args.task)
    role = args.role
    agent = task["assigned_agent"] if role == "worker" else task["reviewer"]
    if agent not in ADAPTERS:
        raise Refused(f"'{agent}' has no CLI adapter; run that step by hand and record a review_note")
    adapter = ADAPTERS[agent]
    timeout = args.timeout or DEFAULT_TIMEOUT[role]

    last_review, worker_run, patch, checks = None, None, None, None
    if role == "worker":
        if task["status"] not in ("ready", "rework", "in_progress"):
            raise Refused(f"worker run needs status ready, rework or in_progress (is {task['status']})")
        base = git("rev-parse", "--verify", args.base + "^{commit}")
        prev = latest(task, "reviewer")
        if task["status"] == "rework" and prev:
            last_review = json.loads((RUNS / task["task_id"] / prev["run_id"] / "review.json").read_text())
        prompt = worker_prompt(task, last_review)
    else:
        if task["status"] != "review":
            raise Refused(f"reviewer run needs status review (is {task['status']})")
        worker_run = latest(task, "worker")
        if not worker_run:
            raise Refused("no completed worker run to review")
        reviewer_path = adapter.locate()
        same = worker_run["cli"]["name"] == adapter.cli or (
            reviewer_path and worker_run["cli"]["path"]
            and os.path.realpath(reviewer_path) == os.path.realpath(worker_run["cli"]["path"]))
        if same:
            raise Refused(f"worker and reviewer would run the same executable ({worker_run['cli']['path']}); "
                          "different labels on one executable are not independence")
        base = worker_run["result_commit"]
        has_command = any(c["verification"]["method"] == "command" for c in task["acceptance_criteria"])
        check_run = latest(task, "check", target=base)
        if has_command and not check_run:
            raise Refused(f"run `aiwos.py check` on {base[:12]} before review")
        if check_run:
            checks = json.loads((RUNS / task["task_id"] / check_run["run_id"] / "checks.json").read_text())
        patch = (RUNS / task["task_id"] / worker_run["run_id"] / "patch.diff").read_bytes()
        prompt = reviewer_prompt(task, worker_run, patch, checks)

    cli_path, cli = cli_record(adapter)
    auth, refusal = adapter.auth(cli_path) if cli_path else ({"checked": False, "logged_in": None,
                                                              "method": None, "provider": None},
                                                             f"'{adapter.cli}' not found on PATH")
    run_id = f"{task['task_id']}-{role}-{stamp()}"
    run_dir = RUNS / task["task_id"] / run_id
    ws_root = workspace_root() / run_id
    argv = adapter.argv(cli_path or adapter.cli, role, ws_root / "tree", ws_root / "out", REVIEW_SCHEMA, args.model)

    if not args.execute:
        print(json.dumps({"dry_run": True, "task": task["task_id"], "role": role, "agent": agent, "cli": cli,
                          "auth": auth, "refusal": refusal, "argv": argv, "stdin": "prompt (below)",
                          "base_or_target_commit": base, "workspace": str(ws_root / "tree"),
                          "agent_user": isolation.agent_user(),
                          "timeout_s": timeout, "prompt_sha256": sha256(prompt.encode())},
                         ensure_ascii=False, indent=2))
        print("\n----- prompt -----\n" + prompt)
        print("dry run: nothing was executed. Add --execute to run.")
        return EXIT_BLOCKED if refusal else EXIT_OK

    with active_lock(task["task_id"], run_id):
        run_dir.mkdir(parents=True)
        rec = new_record(task, path, task_sha, run_id, role, agent, cli, timeout)
        rec.update(auth=auth, argv=argv, model_requested=args.model)
        rec["base_commit"] = base if role == "worker" else worker_run["base_commit"]
        rec["target_commit"] = None if role == "worker" else base
        if refusal:
            rec["stop_reason"] = refusal
            finish(run_dir, rec)
            print(f"BLOCKED {run_id}: {refusal}\n(record: {rel(run_dir / 'run.json')}; task not changed)")
            return EXIT_BLOCKED

        if role == "worker" and task["status"] != "in_progress":
            transition(task, "in_progress", f"worker run {run_id} started")
            task_sha = write_task(path, task, task_sha)

        ws, ws_base = make_workspace(base, run_id, writable=(role == "worker"))
        store(run_dir, rec, "prompt.md", prompt.encode())
        print(f"running {agent} ({cli['version']}) as {role} in {ws.tree} (timeout {timeout}s) ...", flush=True)
        state, code, out, err, duration = execute(argv, ws.tree, adapter.env(), prompt, timeout)
        parsed = adapter.parse(out.decode("utf-8", errors="replace"), ws.out)
        rec.update(exit_code=code, duration_s=duration, models_reported=parsed["models_reported"],
                   session_id_reported=parsed["session_id"], cost_usd_reported=parsed["cost_usd"],
                   num_turns_reported=parsed["num_turns"])

        leaked = scan(rec, "stdout", out) | scan(rec, "stderr", err)
        data = isolation.read_untrusted(ws.out / "last-message.txt", OUTPUT_CAP)  # agent-written: untrusted
        if data is not None:
            leaked |= scan(rec, "last-message.txt", data)
            if not leaked:
                store(run_dir, rec, "last-message.txt", data)

        if role == "worker":
            ws.git("add", "-A")
            new_patch = ws.git("diff", "--cached", "--binary", ws_base, raw=True)
            rec["changed_files"] = ws.git("diff", "--cached", "--name-only", ws_base).splitlines()
            leaked |= scan(rec, "patch.diff", new_patch)
            rec["security"]["out_of_scope_paths"] = out_of_scope(task, rec["changed_files"])
        else:
            modified = bool(ws.git("status", "--porcelain")) or ws.git("rev-parse", "HEAD") != ws_base
            rec["security"]["workspace_modified_by_reviewer"] = modified

        out_name = "agent-stdout.jsonl" if adapter.cli == "codex" else "agent-stdout.json"
        if not leaked:
            store(run_dir, rec, out_name, out)
            store(run_dir, rec, "agent-stderr.txt", err)

        review = parsed["structured"] if role == "reviewer" else None
        review_problem = None
        if role == "reviewer" and not leaked:
            from jsonschema import Draft202012Validator
            schema = json.loads(REVIEW_SCHEMA.read_text())
            if review is None:
                review_problem = "reviewer returned no structured output"
            else:
                problems = [e.message for e in Draft202012Validator(schema).iter_errors(review)]
                review_problem = f"review output does not match schema: {problems[0]}" if problems else None

        if leaked:
            rec["status"], rec["stop_reason"] = "security_violation", "credential-shaped text in agent output"
        elif state != "exited":
            rec["status"], rec["stop_reason"] = state, f"{state} after {duration}s"
        elif code != 0 or parsed["is_error"]:
            rec["status"], rec["stop_reason"] = "failed", f"exit code {code}, is_error={parsed['is_error']}"
        elif role == "worker" and rec["security"]["out_of_scope_paths"]:
            rec["status"], rec["stop_reason"] = "scope_violation", "changes outside allowed_paths"
        elif role == "worker" and not rec["changed_files"]:
            rec["status"], rec["stop_reason"] = "failed", "worker produced no changes"
        elif role == "reviewer" and rec["security"]["workspace_modified_by_reviewer"]:
            rec["status"], rec["stop_reason"] = "security_violation", "reviewer modified the tree it reviewed"
        elif review_problem:
            rec["status"], rec["stop_reason"] = "failed", review_problem
        else:
            rec["status"] = "completed"

        if role == "worker" and not leaked:
            store(run_dir, rec, "patch.diff", new_patch)
            if rec["status"] == "completed":
                rec["branch"] = f"run/{task['task_id']}/{run_id}"
                rec["result_commit"] = commit_patch(
                    base, new_patch, rec["branch"],
                    f"{task['task_id']}: {agent} worker run {run_id}\n\nRecorded by aiwos-runner; not reviewed.")
        if review is not None and not leaked:
            store(run_dir, rec, "review.json", (json.dumps(review, ensure_ascii=False, indent=2) + "\n").encode())

        run_sha = finish(run_dir, rec)
        return record_outcome(path, task, task_sha, rec, run_dir, run_sha, review)


def record_outcome(path, task, task_sha, rec, run_dir, run_sha, review):
    """Attach the run to the contract and move the state. Conflicts stop here:
    the run record stays on disk, the contract is left for a human."""
    role, ev = rec["role"], rec["run_id"].rsplit("-", 1)[1]
    prefix = {"worker": "W", "reviewer": "R", "check": "C"}[role] + ev
    add_evidence(task, prefix, "run_record", run_dir / "run.json", run_sha, "aiwos-runner",
                 summary=f"{role} run by {rec['agent']}: {rec['status']}")
    status = rec["status"]
    if role == "worker" and "patch.diff" in rec["files"]:
        add_evidence(task, prefix + "-diff", "diff", run_dir / "patch.diff", rec["files"]["patch.diff"], rec["agent"])
    if role == "reviewer" and status == "completed":
        ids = [c["criterion_id"] for c in review["criteria"]
               if c["criterion_id"] in {a["id"] for a in task["acceptance_criteria"]}]
        add_evidence(task, prefix + "-review", "review_note", run_dir / "review.json",
                     rec["files"]["review.json"], rec["agent"], ids, summary=review["summary"][:500])

    if status in ("security_violation", "scope_violation"):
        escalate(task, f"{rec['run_id']}: {status} — {rec['stop_reason']}")
    elif role == "worker" and status == "completed":
        transition(task, "review", f"worker run {rec['run_id']} completed; result {rec['result_commit'][:12]}")
    elif role == "worker" and status == "interrupted":
        transition(task, "suspended", f"worker run {rec['run_id']} interrupted")
    elif role == "worker":
        escalate(task, f"{rec['run_id']}: worker run {status} — {rec['stop_reason']}")
    try:
        write_task(path, task, task_sha)
    except (Conflict, Refused) as exc:
        print(f"CONFLICT: run recorded at {rel(run_dir)} but the contract was not updated: {exc}")
        return EXIT_CONFLICT
    print(f"{status.upper()} {rec['run_id']} -> task {task['task_id']} is now '{task['status']}'"
          + (f"\n  branch {rec['branch']} @ {rec['result_commit']}" if rec["result_commit"] else "")
          + (f"\n  stop: {rec['stop_reason']}" if rec["stop_reason"] else ""))
    if status in ("security_violation", "scope_violation"):
        return EXIT_SECURITY
    return EXIT_OK if status == "completed" else EXIT_NOT_COMPLETED


def cmd_check(args):
    path, task, task_sha = load_valid(args.task)
    if task["status"] != "review":
        raise Refused(f"check needs status review (is {task['status']})")
    worker_run = latest(task, "worker")
    if not worker_run:
        raise Refused("no completed worker run to check")
    target = worker_run["result_commit"]
    commands = [c for c in task["acceptance_criteria"] if c["verification"]["method"] == "command"]
    timeout = args.timeout or DEFAULT_TIMEOUT["check"]
    if not args.execute:
        for c in commands:
            print(f"{c['id']}: bash -c {c['verification']['command']!r} "
                  f"(expect exit {c['verification'].get('expected_exit_code', 0)}, timeout {timeout}s)")
        print(f"dry run on {target}: nothing was executed. Add --execute to run.")
        return EXIT_OK
    run_id = f"{task['task_id']}-check-{stamp()}"
    run_dir = RUNS / task["task_id"] / run_id
    with active_lock(task["task_id"], run_id):
        run_dir.mkdir(parents=True)
        bash = subprocess.run(["bash", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
        rec = new_record(task, path, task_sha, run_id, "check", "aiwos-runner",
                         {"name": "bash", "path": subprocess.run(["sh", "-c", "command -v bash"], capture_output=True,
                                                                 text=True).stdout.strip() or None,
                          "version": bash[:120]}, timeout)
        rec.update(base_commit=worker_run["base_commit"], target_commit=target,
                   argv=["bash", "-c", "<criterion command>"])
        ws, _ = make_workspace(target, run_id, writable=True)
        results, leaked, started = [], False, time.monotonic()
        for c in commands:
            expected = c["verification"].get("expected_exit_code", 0)
            # acceptance commands run code the agent wrote: as the agent user too
            state, code, out, err, duration = execute(["bash", "-c", c["verification"]["command"]], ws.tree,
                                                      scrubbed_env(), None, timeout)
            log = out + (b"\n--- stderr ---\n" + err if err else b"")
            if scan(rec, f"checks/{c['id']}.log", log):
                leaked = True
                continue
            store(run_dir, rec, f"checks/{c['id']}.log", log)
            results.append({"criterion_id": c["id"], "command": c["verification"]["command"],
                            "expected_exit_code": expected, "exit_code": code if state == "exited" else None,
                            "state": state, "passed": state == "exited" and code == expected,
                            "duration_s": duration, "log": f"checks/{c['id']}.log"})
        rec["duration_s"] = round(time.monotonic() - started, 1)
        store(run_dir, rec, "checks.json", (json.dumps(results, ensure_ascii=False, indent=2) + "\n").encode())
        rec["status"] = "security_violation" if leaked else "completed"
        rec["stop_reason"] = "credential-shaped text in a check log" if leaked else None
        rec["exit_code"] = 0
        run_sha = finish(run_dir, rec)
        ev = run_id.rsplit("-", 1)[1]
        add_evidence(task, "C" + ev, "run_record", run_dir / "run.json", run_sha, "aiwos-runner",
                     summary=f"check run: {sum(r['passed'] for r in results)}/{len(results)} passed")
        for r in results:
            # A command that timed out has no exit code; it is recorded as a
            # log, not given an invented one.
            kind = "command_output" if r["exit_code"] is not None else "log"
            add_evidence(task, f"C{ev}-{r['criterion_id']}", kind, run_dir / r["log"],
                         rec["files"][r["log"]], "aiwos-runner", [r["criterion_id"]],
                         command=r["command"], exit_code=r["exit_code"],
                         summary=f"{r['state']}, expected exit {r['expected_exit_code']}: "
                                 f"{'PASS' if r['passed'] else 'FAIL'}")
        if leaked:
            escalate(task, f"{run_id}: security_violation — credential-shaped text in a check log")
        try:
            write_task(path, task, task_sha)
        except (Conflict, Refused) as exc:
            print(f"CONFLICT: check recorded at {rel(run_dir)} but the contract was not updated: {exc}")
            return EXIT_CONFLICT
    for r in results:
        print(f"{r['criterion_id']}: {'PASS' if r['passed'] else 'FAIL'} (exit {r['exit_code']}, "
              f"expected {r['expected_exit_code']})")
    return EXIT_SECURITY if leaked else EXIT_OK


def judgement(task):
    """Compute verdicts from recorded runs only. Mechanical results decide
    command criteria; the reviewer decides inspection criteria; 'human'
    criteria stay UNVERIFIED until a human records a review."""
    worker_run = latest(task, "worker")
    if not worker_run:
        raise Refused("no completed worker run")
    head = worker_run["result_commit"]
    check_run, review_run = latest(task, "check", target=head), latest(task, "reviewer", target=head)
    checks, review = {}, None
    if check_run:
        checks = {c["criterion_id"]: c for c in json.loads(
            (RUNS / task["task_id"] / check_run["run_id"] / "checks.json").read_text())}
    if review_run:
        review = json.loads((RUNS / task["task_id"] / review_run["run_id"] / "review.json").read_text())
    reviewer_says = {c["criterion_id"]: c for c in (review or {}).get("criteria", [])}

    ev_ids = lambda run: [e["id"] for e in evidence_for_run(task, run)] if run else []  # noqa: E731
    per_criterion = {}
    for c in task["acceptance_criteria"]:
        method = c["verification"]["method"]
        if method == "command":
            r = checks.get(c["id"])
            verdict = "UNVERIFIED" if r is None else ("PASS" if r["passed"] else "FAIL")
            source = "check"
            evidence = [e["id"] for e in evidence_for_run(task, check_run) if c["id"] in e["criterion_ids"]] \
                if check_run else []
        elif method == "inspection" and c["id"] in reviewer_says:
            verdict, source, evidence = reviewer_says[c["id"]]["verdict"], "reviewer", ev_ids(review_run)
        else:
            verdict, source, evidence = "UNVERIFIED", "none", []
        per_criterion[c["id"]] = {"dimension": c["dimension"], "verdict": verdict, "source": source,
                                  "evidence": evidence}

    blocking = [d for d in (review or {}).get("defects", []) if d["severity"] in BLOCKING_SEVERITIES]
    quality = {}
    for dim in vt.DIMENSIONS:
        items = {k: v for k, v in per_criterion.items() if v["dimension"] == dim}
        verdicts = {v["verdict"] for v in items.values()}
        dim_blocking = [d for d in blocking if d["dimension"] == dim]
        if "FAIL" in verdicts or dim_blocking:
            verdict = "FAIL"
        elif not items or "UNVERIFIED" in verdicts:
            verdict = "UNVERIFIED"
        else:
            verdict = "PASS"
        sources = {v["source"] for v in items.values()}
        evidence = sorted({e for v in items.values() for e in v["evidence"]})
        if verdict != "UNVERIFIED" and "reviewer" in sources:
            judged_by = review_run["agent"]
        elif verdict != "UNVERIFIED":
            judged_by = "aiwos-runner"
        else:
            judged_by = None
        note = "; ".join(f"{k}: {v['verdict']} ({v['source']})" for k, v in items.items()) or "no criteria"
        if dim_blocking:
            note += f"; {len(dim_blocking)} blocking defect(s) from review"
        quality[dim] = {"verdict": verdict, "evidence_ids": evidence if verdict != "UNVERIFIED" else [],
                        "judged_by": judged_by, "note": note}
    return worker_run, check_run, review_run, review, per_criterion, quality


def cmd_judge(args):
    path, task, task_sha = load_valid(args.task)
    if task["status"] != "review":
        raise Refused(f"judge needs status review (is {task['status']})")
    worker_run, check_run, review_run, review, per_criterion, quality = judgement(task)
    verdicts = {q["verdict"] for q in quality.values()}
    if verdicts == {"PASS"}:
        decision = "passed"
    elif "FAIL" in verdicts:
        decision = "rework" if task["retry_count"] < task["max_retries"] else "escalated"
    else:
        decision = "review"
    print(f"worker {worker_run['run_id']} @ {worker_run['result_commit'][:12]}; "
          f"check {check_run['run_id'] if check_run else '—'}; review {review_run['run_id'] if review_run else '—'}")
    for cid, v in per_criterion.items():
        print(f"  {cid:<5} {v['dimension']:<17} {v['verdict']:<10} from {v['source']}")
    for dim, q in quality.items():
        print(f"  {dim:<17} {q['verdict']:<10} judged_by={q['judged_by']}")
    print(f"decision: {task['status']} -> {decision}")
    if not args.write:
        print("dry run: contract not changed. Add --write to record.")
        return EXIT_OK
    task["quality"] = quality
    if decision == "rework":
        task["retry_count"] += 1
        transition(task, "rework", f"judge: FAIL in {sorted(d for d, q in quality.items() if q['verdict'] == 'FAIL')}")
    elif decision == "escalated":
        escalate(task, f"retry limit {task['max_retries']} reached with FAIL verdicts")
    elif decision == "passed":
        transition(task, "passed", f"judge: all three dimensions PASS on {worker_run['result_commit'][:12]}")
    try:
        write_task(path, task, task_sha)
    except (Conflict, Refused) as exc:
        print(f"NOT RECORDED: {exc}")
        return EXIT_CONFLICT if isinstance(exc, Conflict) else EXIT_INVALID
    print(f"recorded: task {task['task_id']} is now '{task['status']}'")
    return EXIT_OK


def cmd_transition(args):
    path, task, task_sha = load_valid(args.task)
    if args.to in vt.HUMAN_ONLY_TARGETS:
        # Security Fix 001: whoever runs this CLI can type `--by human`. Final
        # approval waits for an authenticated channel; until then it is refused.
        raise Refused(f"'{args.to}' needs a verified human approval, and the caller of this CLI cannot be "
                      "verified (fail closed). See docs/human-approval.md")
    previous = task["status"]
    # The actor is whatever the caller typed; the record says so.
    note = f"{args.reason or 'manual transition'} [actor '{args.by}' declared on the CLI, unverified]"
    task["history"].append({"from": previous, "to": args.to, "at": now(), "by": args.by, "note": note})
    task["status"] = args.to
    if args.to == "escalated":
        task["escalation_reason"] = args.reason
    elif previous == "escalated":
        task["escalation_reason"] = None
    try:
        write_task(path, task, task_sha)
    except Refused as exc:
        print(exc)
        return EXIT_INVALID
    print(f"{task['task_id']}: {previous} -> {args.to} (by {args.by})")
    return EXIT_OK


def cmd_status(args):
    path, task, _ = load_valid(args.task)
    print(f"{task['task_id']} [{task['task_type']}] status={task['status']} "
          f"retry={task['retry_count']}/{task['max_retries']} worker={task['assigned_agent']} "
          f"reviewer={task['reviewer']}")
    for dim, q in task["quality"].items():
        print(f"  {dim:<17} {q['verdict']:<10} judged_by={q['judged_by']}")
    for _, r in run_records(task):
        print(f"  run {r['run_id']:<40} {r['role']:<8} {r['agent']:<12} {r['status']:<18} "
              f"{(r['result_commit'] or r['target_commit'] or '')[:12]}")
    nxt = {"ready": "run --role worker", "rework": "run --role worker", "in_progress": "run --role worker (restart)",
           "review": "check, then run --role reviewer, then judge", "passed": "final approval by a verified human (not implemented; docs/human-approval.md)",
           "escalated": "human decision (transition --to ready or cancelled)",
           "suspended": "transition back to the state before suspension"}.get(task["status"], "none")
    print(f"next: {nxt}")
    return EXIT_OK


def metrics_rows():
    records = [json.loads(p.read_text()) for p in sorted(RUNS.glob("*/*/run.json"))]
    tasks = {}
    rows = []
    for w in [r for r in records if r["role"] == "worker"]:
        head = w["result_commit"]
        check = next((r for r in reversed(records) if r["role"] == "check" and head and r["target_commit"] == head
                      and r["status"] == "completed"), None)
        review = next((r for r in reversed(records) if r["role"] == "reviewer" and head
                       and r["target_commit"] == head and r["status"] == "completed"), None)
        checks = json.loads((RUNS / w["task_id"] / check["run_id"] / "checks.json").read_text()) if check else None
        review_doc = json.loads((RUNS / w["task_id"] / review["run_id"] / "review.json").read_text()) if review else None
        if w["task_path"] not in tasks:
            try:
                tasks[w["task_path"]] = json.loads((ROOT / w["task_path"]).read_text())["status"]
            except (OSError, json.JSONDecodeError, KeyError):
                tasks[w["task_path"]] = None
        rows.append({
            "run_id": w["run_id"], "task_id": w["task_id"], "task_type": w["task_type"], "agent": w["agent"],
            "cli_version": w["cli"]["version"],
            "models_reported": [m["model"] for m in w["models_reported"]] if w["models_reported"] else None,
            "duration_s": w["duration_s"], "retry_count_at_start": w["retry_count_at_start"],
            "run_status": w["status"],
            "checks_passed": sum(c["passed"] for c in checks) if checks is not None else None,
            "checks_total": len(checks) if checks is not None else None,
            "reviewer_agent": review["agent"] if review else None,
            "defects": len(review_doc["defects"]) if review_doc else None,
            "blocking_defects": sum(d["severity"] in BLOCKING_SEVERITIES for d in review_doc["defects"])
            if review_doc else None,
            "task_status_now": tasks[w["task_path"]],
        })
    blocked = [{"run_id": r["run_id"], "agent": r["agent"], "role": r["role"], "reason": r["stop_reason"]}
               for r in records if r["status"] == "blocked"]
    return rows, blocked


def cmd_metrics(args):
    rows, blocked = metrics_rows()
    if args.json:
        print(json.dumps({"worker_runs": rows, "blocked_attempts": blocked}, ensure_ascii=False, indent=2))
        return EXIT_OK
    show = lambda v: "—" if v is None else str(v)  # noqa: E731  missing is not zero
    print("| run | type | agent | models | 秒 | 修正回数 | 受入コマンド | 欠陥(重大) | run | task |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        rate = "—" if r["checks_total"] is None else f"{r['checks_passed']}/{r['checks_total']}"
        defects = "—" if r["defects"] is None else f"{r['defects']}({r['blocking_defects']})"
        print(f"| {r['run_id']} | {r['task_type']} | {r['agent']} | {show(r['models_reported'] and ','.join(r['models_reported']))} "
              f"| {show(r['duration_s'])} | {r['retry_count_at_start']} | {rate} | {defects} | {r['run_status']} "
              f"| {show(r['task_status_now'])} |")
    for b in blocked:
        print(f"blocked: {b['run_id']} ({b['agent']}, {b['role']}): {b['reason']}")
    print("— = not measured (no record), never 0. Rates are per run; runs are not pooled across tasks.")
    return EXIT_OK


def main(argv):
    p = argparse.ArgumentParser(prog="aiwos.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth")
    r = sub.add_parser("run")
    r.add_argument("task")
    r.add_argument("--role", choices=("worker", "reviewer"), required=True)
    r.add_argument("--execute", action="store_true")
    r.add_argument("--base", default="HEAD", help="commit the worker starts from (worker only)")
    r.add_argument("--model", default=None)
    r.add_argument("--timeout", type=int, default=None)
    c = sub.add_parser("check")
    c.add_argument("task")
    c.add_argument("--execute", action="store_true")
    c.add_argument("--timeout", type=int, default=None)
    j = sub.add_parser("judge")
    j.add_argument("task")
    j.add_argument("--write", action="store_true")
    t = sub.add_parser("transition")
    t.add_argument("task")
    t.add_argument("--to", required=True)
    t.add_argument("--by", required=True)
    t.add_argument("--reason")
    s = sub.add_parser("status")
    s.add_argument("task")
    m = sub.add_parser("metrics")
    m.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    handler = {"auth": cmd_auth, "run": cmd_run, "check": cmd_check, "judge": cmd_judge,
               "transition": cmd_transition, "status": cmd_status, "metrics": cmd_metrics}[args.cmd]
    try:
        return handler(args)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_INVALID if "not a valid contract" in str(exc) else EXIT_BLOCKED
    except Conflict as exc:
        print(f"CONFLICT: {exc}", file=sys.stderr)
        return EXIT_CONFLICT


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
