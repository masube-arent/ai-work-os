"""One worker process: lease a task, run one stage, record and publish it.

A worker never runs code from the task branch. It runs scripts/aiwos.py from
the trusted code_dir with AIWOS_ROOT pointing at its own worktree, so a change
an agent made on the branch cannot rewrite the rules it is judged by.
"""
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import gitops, stages
from .procs import kill_tree
from .store import MODEL_STAGES, Store

# Exit codes of scripts/aiwos.py (core/execution.md).
EXIT_OK, EXIT_NOT_COMPLETED, EXIT_INVALID, EXIT_BLOCKED, EXIT_CONFLICT, EXIT_SECURITY = 0, 1, 2, 3, 4, 5
# Variables the worker itself may hold but no child process gets.
SECRET_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
              "CODEX_API_KEY")
# Usage-limit wording. Heuristic: matched against the stage's own output only.
RATE_LIMIT = re.compile(r"rate[ _-]?limit|usage limit|quota exceeded|too many requests|\b429\b", re.I)
OUTPUT_TAIL = 4000


def stage_env(cfg, worktree, worker_id):
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    env["AIWOS_ROOT"] = str(worktree)
    env["AIWOS_WORKSPACES"] = str(Path(cfg["state_dir"]) / "workspaces" / f"w{worker_id:02d}")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if cfg["extra_path"]:
        env["PATH"] = os.pathsep.join(cfg["extra_path"] + [env.get("PATH", "")])
    for name, path in cfg["agent_bins"].items():
        env[{"claude": "AIWOS_CLAUDE_BIN", "codex": "AIWOS_CODEX_BIN"}[name]] = path
    return env


class Worker:
    def __init__(self, cfg, worker_id):
        self.cfg, self.id, self.pid = cfg, worker_id, os.getpid()
        self.parent = os.getppid()
        state = Path(cfg["state_dir"])
        self.store = Store(str(state / "aiwos.db"))
        self.worktree = state / "worktrees" / f"w{worker_id:02d}"
        self.logdir = state / "logs"
        self.stopping = False
        self.child = None
        self.log = logging.getLogger(f"worker-{worker_id:02d}")
        handler = logging.FileHandler(self.logdir / f"worker-{worker_id:02d}.log")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.log.addHandler(handler)
        self.log.setLevel(logging.INFO)

    # ---------------------------------------------------------------- lifecycle

    def on_signal(self, signum, frame):
        self.stopping = True
        if self.child and self.child.poll() is None:
            try:  # the runner turns SIGINT into an 'interrupted' record and stops its agent
                os.kill(self.child.pid, signal.SIGINT)
            except ProcessLookupError:
                pass

    def orphaned(self):
        return os.getppid() != self.parent  # the controller is gone; stop rather than run unsupervised

    def run(self):
        signal.signal(signal.SIGTERM, self.on_signal)
        signal.signal(signal.SIGINT, self.on_signal)
        self.store.worker_started(self.id, self.pid)
        cfg = self.cfg
        gitops.reset_worktree(str(self.worktree))  # created by the controller before workers start
        self.log.info("started pid=%s", self.pid)
        while not self.stopping and not self.orphaned():
            self.store.heartbeat(self.id, self.pid, cfg["lease_ttl_s"])
            task = self.store.claim(self.id, self.pid, cfg["lease_ttl_s"], self.store.usable_agents())
            if task is None:
                self.sleep(cfg["poll_interval_s"])
                continue
            try:
                self.handle(task)
            except Exception as exc:  # never let one task kill the worker loop
                self.log.exception("task %s stage %s failed in the worker", task["task_id"], task["stage"])
                self.store.finish(self.id, self.pid, task["task_id"], stage=task["stage"], agent=task["agent"],
                                  state="queued", reason=f"worker error: {type(exc).__name__}: {str(exc)[:300]}",
                                  delay=60, reset_attempts=False)
                self.safe_reset()
        self.log.info("stopping")
        return 0

    def sleep(self, seconds):
        end = time.time() + seconds
        while time.time() < end and not self.stopping and not self.orphaned():
            time.sleep(min(0.5, end - time.time()))

    def safe_reset(self):
        try:
            gitops.reset_worktree(str(self.worktree))
        except Exception:
            self.log.exception("worktree reset failed")

    # ---------------------------------------------------------------- one stage

    def handle(self, task):
        cfg, tid, stage = self.cfg, task["task_id"], task["stage"]
        ai_run = stage in MODEL_STAGES
        if ai_run and task["ai_runs"] >= cfg["max_ai_runs_per_task"]:
            self.store.finish(self.id, self.pid, tid, stage=stage, agent=task["agent"], state="held",
                              reason=f"AI run budget spent ({task['ai_runs']}/{cfg['max_ai_runs_per_task']})")
            return
        gitops.reset_worktree(str(self.worktree))
        gitops.checkout_task_branch(cfg["repo_dir"], str(self.worktree), tid, cfg["remote"], cfg["base_branch"])
        timeout = cfg["stage_timeout_s"][stage]
        argv = [sys.executable, str(Path(cfg["code_dir"]) / "scripts" / "aiwos.py"),
                *stages.command(stage, task["contract_path"], timeout)]
        logfile = self.logdir / "stages" / tid / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{stage}.log"
        logfile.parent.mkdir(parents=True, exist_ok=True)
        self.log.info("task %s: stage %s (attempt %s)", tid, stage, task["attempts"])
        code, lost, timed_out = self.execute(task, argv, logfile, timeout + cfg["kill_grace_s"] + 60)
        if lost:
            # someone reclaimed the lease: our result does not count and is not committed
            self.log.warning("task %s: lease lost during %s; discarding", tid, stage)
            self.safe_reset()
            return
        output = logfile.read_text(errors="replace")[-OUTPUT_TAIL:] if logfile.exists() else ""
        if timed_out or self.stopping:
            self.safe_reset()
            self.store.finish(self.id, self.pid, tid, stage=stage, agent=task["agent"], state="queued",
                              reason="stopped" if self.stopping else f"stage killed after {timeout}s + grace",
                              ai_run=ai_run, delay=0 if self.stopping else 60, reset_attempts=False)
            return

        contract_path = task["contract_path"]
        commit = gitops.commit_all(str(self.worktree), ["runs", contract_path],
                                   f"aiwos({tid}): {stage} exit {code}")
        contract = json.loads((self.worktree / contract_path).read_text(encoding="utf-8"))
        nxt, agent, state, reason = stages.next_stage(
            contract, lambda p: (self.worktree / p).read_text(encoding="utf-8"), after=stage)
        delay, detail = 0, {"exit": code, "commit": commit, "status": contract["status"]}

        if code == EXIT_BLOCKED and task["agent"]:
            # not logged in / unverified auth: park every stage of this agent, re-check later
            self.store.set_agent_status(task["agent"], False, output.strip().splitlines()[-1][:300] if output else "blocked")
            nxt, agent, state, reason = stage, task["agent"], "waiting", f"{task['agent']} unusable: see agent status"
            delay = cfg["auth_recheck_s"]
        elif code == EXIT_CONFLICT:
            nxt, agent, state, reason, delay = stage, task["agent"], "queued", "contract update conflict", 60
        elif code in (EXIT_INVALID, EXIT_BLOCKED):
            nxt, agent, state, reason = stage, task["agent"], "held", f"runner refused (exit {code}): {output[-300:]}"
        elif ai_run and code != EXIT_OK and RATE_LIMIT.search(self.agent_output(contract, output)):
            wait = self.store.backoff(task["agent"], cfg["backoff_initial_s"], cfg["backoff_max_s"],
                                      f"usage limit reported during {tid} {stage}")
            detail["backoff_s"] = wait
            if state == "queued":  # e.g. a failed review: same stage again once the backoff ends
                state, delay = "waiting", wait
        elif ai_run and code == EXIT_OK:
            self.store.clear_backoff(task["agent"])

        if state == "queued" and delay:
            state = "waiting"
        publish = self.publish(task, contract) if commit else {"published": False, "reason": "nothing to commit"}
        detail.update(publish)
        self.store.finish(self.id, self.pid, tid, stage=nxt or stage, agent=agent, state=state, reason=reason,
                          ai_run=ai_run, delay=delay, detail=detail)
        gitops.git(str(self.worktree), "checkout", "--detach", "--quiet")  # free the branch for other workers
        self.log.info("task %s: %s -> %s/%s (%s)", tid, stage, state, nxt, reason)

    def execute(self, task, argv, logfile, hard_timeout):
        """Run the stage, renewing the lease; kill the whole tree on timeout,
        on stop, or when the lease is taken away."""
        env = stage_env(self.cfg, self.worktree, self.id)
        with open(logfile, "wb") as log:
            self.child = subprocess.Popen(argv, cwd=self.worktree, env=env, stdout=log, stderr=subprocess.STDOUT,
                                          start_new_session=True)
            self.store.set_child(self.id, self.pid, task["task_id"], self.child.pid)
            start, beat, lost, timed_out = time.time(), 0.0, False, False
            while self.child.poll() is None:
                time.sleep(0.2)
                if time.time() - beat >= self.cfg["heartbeat_s"]:
                    beat = time.time()
                    if not self.store.heartbeat(self.id, self.pid, self.cfg["lease_ttl_s"], task["task_id"]):
                        lost = True
                if lost or self.orphaned() or time.time() - start > hard_timeout:
                    timed_out = not lost
                    kill_tree(self.child.pid, grace=self.cfg["kill_grace_s"], first=signal.SIGINT)
                    break
            if self.stopping and self.child.poll() is None:
                kill_tree(self.child.pid, grace=self.cfg["kill_grace_s"], first=signal.SIGINT)
            code = self.child.wait()
            self.child = None
        return code, lost, timed_out

    def agent_output(self, contract, stage_output):
        """Stage output plus the newest run's stored agent output (both already
        secret-scanned by the runner before they were written)."""
        texts = [stage_output]
        runs = [e for e in contract.get("evidence", []) if e.get("kind") == "run_record"]
        if runs:
            run_dir = (self.worktree / runs[-1]["path"]).parent
            for name in ("agent-stdout.json", "agent-stdout.jsonl", "agent-stderr.txt"):
                p = run_dir / name
                if p.is_file():
                    texts.append(p.read_text(errors="replace")[-20000:])
        return "\n".join(texts)

    def publish(self, task, contract):
        cfg = self.cfg
        title, body = gitops.pr_text(task["task_id"], contract)
        try:
            return gitops.publish(cfg["repo_dir"], cfg["publish"], cfg["remote"], cfg["base_branch"], task,
                                  title, body)
        except Exception as exc:
            self.log.warning("publish failed for %s: %s", task["task_id"], exc)
            return {"published": False, "publish_error": str(exc)[:300]}


def main(cfg, worker_id):
    return Worker(cfg, worker_id).run()
