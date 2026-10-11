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
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import gitops, stages
from .publishing import publish_allowed
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
GIT_ALLOWANCE_S = 1200  # checkout + commit + push (git timeouts: 300 s per command, 600 s for push)


class LeaseKeeper(threading.Thread):
    """Renews one lease every heartbeat_s for as long as the worker handles the
    task (checkout, stage, commit, push), on its own SQLite connection. If a
    renewal finds the lease gone, `lost` is set and the worker discards its work.
    A frozen worker process renews nothing, so its lease expires and the
    controller stops it before handing the task on."""

    # Renewal stops after max_duration (the longest a stage may legitimately take:
    # its hard timeout plus checkout, commit and push). A worker whose main thread
    # is stuck past that lets its lease expire, and the controller stops it.

    def __init__(self, db_path, worker_id, pid, task_id, ttl, interval, max_duration):
        super().__init__(daemon=True, name=f"lease-{task_id}")
        self.args = (db_path, worker_id, pid, task_id, ttl)
        self.interval = interval
        self.deadline = time.time() + max_duration
        self.lost = threading.Event()
        self.done = threading.Event()

    def run(self):
        db_path, worker_id, pid, task_id, ttl = self.args
        store = Store(db_path)
        try:
            while not self.done.wait(self.interval):
                if time.time() > self.deadline:
                    return  # stop renewing: let the controller deal with a stuck worker
                try:
                    if not store.heartbeat(worker_id, pid, ttl, task_id):
                        self.lost.set()
                        return
                except sqlite3.Error:
                    continue  # busy database: the next round renews in time (ttl > 3 x interval)
        finally:
            store.db.close()


def stage_env(cfg, worktree, worker_id, lease_tag=None):
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    env["AIWOS_ROOT"] = str(worktree)
    if cfg.get("agent_user"):  # Security Fix 002: untrusted commands run as this user
        env["AIWOS_AGENT_USER"], env["AIWOS_AGENT_GROUP"] = cfg["agent_user"], cfg["agent_group"]
        env["AIWOS_AGENT_PYTHON"] = cfg["agent_python"]
    env["AIWOS_WORKSPACES"] = str(Path(cfg["workspaces_dir"]) / f"w{worker_id:02d}")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if cfg["extra_path"]:
        env["PATH"] = os.pathsep.join(cfg["extra_path"] + [env.get("PATH", "")])
    for name, path in cfg["agent_bins"].items():
        env[{"claude": "AIWOS_CLAUDE_BIN", "codex": "AIWOS_CODEX_BIN"}[name]] = path
    if lease_tag:  # lets the controller find this stage's processes even if their pid was never recorded
        env["AIWOS_LEASE_TAG"] = lease_tag
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
        self.keeper = None
        self.model_started = False
        self.model_charged = False
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
            task = self.store.claim(self.id, self.pid, cfg["lease_ttl_s"], self.store.usable_agents(),
                                    max_attempts=cfg["max_stage_attempts"])
            if task is None:
                self.sleep(cfg["poll_interval_s"])
                continue
            self.model_started = False
            self.model_charged = False
            stage_limit = cfg["stage_timeout_s"].get(task["stage"], 900) + cfg["kill_grace_s"] + 60
            self.keeper = LeaseKeeper(self.store.path, self.id, self.pid, task["task_id"], cfg["lease_ttl_s"],
                                      cfg["heartbeat_s"], max_duration=stage_limit + GIT_ALLOWANCE_S)
            self.keeper.start()
            try:
                self.handle(task)
            except gitops.Diverged as exc:
                self.log.warning("task %s: %s", task["task_id"], exc)
                self.store.finish(self.id, self.pid, task["task_id"], stage=task["stage"], agent=task["agent"],
                                  state="held", reason=f"diverged from GitHub; a person must reconcile: {exc}"[:500])
                self.safe_reset()
            except Exception as exc:  # never let one task kill the worker loop
                self.log.exception("task %s stage %s failed in the worker", task["task_id"], task["stage"])
                # counts as an attempt (held at max_stage_attempts) and, if a model
                # was already started, as an AI run
                self.store.finish(self.id, self.pid, task["task_id"], stage=task["stage"], agent=task["agent"],
                                  state="queued", reason=f"worker error: {type(exc).__name__}: {str(exc)[:300]}",
                                  delay=60, reset_attempts=False, max_attempts=cfg["max_stage_attempts"],
                                  ai_run=self.model_started and not self.model_charged)
                self.safe_reset()
            finally:
                self.keeper.done.set()
                self.keeper.join(timeout=30)
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
        model_stage = stage in MODEL_STAGES
        if model_stage and task["ai_runs"] >= cfg["max_ai_runs_per_task"]:
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
        if model_stage:
            # charged before the model process exists: no kill can leave it uncounted,
            # so no finish() below charges it again
            self.store.charge_ai_run(self.id, self.pid, tid)
            self.model_charged = True
        code, lost, timed_out = self.execute(task, argv, logfile, timeout + cfg["kill_grace_s"] + 60)
        if lost or self.keeper.lost.is_set():
            # someone reclaimed the lease: our result does not count and is not committed
            self.log.warning("task %s: lease lost during %s; discarding", tid, stage)
            self.safe_reset()
            return
        output = logfile.read_text(errors="replace")[-OUTPUT_TAIL:] if logfile.exists() else ""
        if timed_out or self.stopping:
            self.safe_reset()
            if self.stopping:  # a shutdown is not the stage's fault: the attempt is refunded
                self.store.finish(self.id, self.pid, tid, stage=stage, agent=task["agent"], state="queued",
                                  reason="stopped", refund_attempt=True)
            else:
                self.store.finish(self.id, self.pid, tid, stage=stage, agent=task["agent"], state="queued",
                                  reason=f"stage killed after {timeout}s + grace", delay=60,
                                  reset_attempts=False, max_attempts=cfg["max_stage_attempts"])
            return

        contract_path = task["contract_path"]
        commit = gitops.commit_all(str(self.worktree), ["runs", contract_path],
                                   f"aiwos({tid}): {stage} exit {code}")
        contract = json.loads((self.worktree / contract_path).read_text(encoding="utf-8"))
        nxt, agent, state, reason = stages.next_stage(
            contract, lambda p: (self.worktree / p).read_text(encoding="utf-8"), after=stage)
        delay, detail = 0, {"exit": code, "commit": commit, "status": contract["status"]}
        retry = False  # True: same stage again, counted against max_stage_attempts

        if code == EXIT_BLOCKED and task["agent"] and self.model_charged:
            self.store.refund_ai_run(self.id, self.pid, tid, "runner refused before starting the agent")
        if code == EXIT_BLOCKED and task["agent"]:
            # not logged in / unverified auth: park every stage of this agent, re-check later
            self.store.set_agent_status(task["agent"], False, output.strip().splitlines()[-1][:300] if output else "blocked")
            nxt, agent, state, reason = stage, task["agent"], "waiting", f"{task['agent']} unusable: see agent status"
            delay = cfg["auth_recheck_s"]
        elif code == EXIT_CONFLICT:
            nxt, agent, state, reason, delay = stage, task["agent"], "queued", "contract update conflict", 60
            retry = True
        elif code in (EXIT_INVALID, EXIT_BLOCKED):
            nxt, agent, state, reason = stage, task["agent"], "held", f"runner refused (exit {code}): {output[-300:]}"
        elif model_stage and code != EXIT_OK and RATE_LIMIT.search(self.agent_output(contract, output)):
            wait = self.store.backoff(task["agent"], cfg["backoff_initial_s"], cfg["backoff_max_s"],
                                      f"usage limit reported during {tid} {stage}")
            detail["backoff_s"] = wait
            if state == "queued":  # e.g. a failed review: same stage again once the backoff ends
                state, delay = "waiting", wait
        elif model_stage and code == EXIT_OK:
            self.store.clear_backoff(task["agent"])

        if state == "queued" and delay:
            state = "waiting"
        if self.keeper.lost.is_set():
            self.log.warning("task %s: lease lost after %s; not publishing", tid, stage)
            self.safe_reset()
            return
        publish = self.publish(task, contract) if commit else {"published": False, "reason": "nothing to commit"}
        self.store.record_publish(tid, publish)
        detail.update(publish)
        self.store.finish(self.id, self.pid, tid, stage=nxt or stage, agent=agent, state=state, reason=reason,
                          delay=delay, detail=detail, reset_attempts=not retry,
                          max_attempts=cfg["max_stage_attempts"])
        gitops.git(str(self.worktree), "checkout", "--detach", "--quiet")  # free the branch for other workers
        self.log.info("task %s: %s -> %s/%s (%s)", tid, stage, state, nxt, reason)

    def execute(self, task, argv, logfile, hard_timeout):
        """Run the stage, renewing the lease; kill the whole tree on timeout,
        on stop, or when the lease is taken away."""
        env = stage_env(self.cfg, self.worktree, self.id, lease_tag=f"{task['task_id']}:{self.pid}")
        with open(logfile, "wb") as log:
            self.child = subprocess.Popen(argv, cwd=self.worktree, env=env, stdout=log, stderr=subprocess.STDOUT,
                                          start_new_session=True)
            self.store.set_child(self.id, self.pid, task["task_id"], self.child.pid)
            self.model_started = task["stage"] in MODEL_STAGES
            start, lost, timed_out = time.time(), False, False
            while self.child.poll() is None:
                time.sleep(0.2)
                lost = self.keeper.lost.is_set()  # renewed by LeaseKeeper
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
        """Publish only verified content, and only when credentials cannot reach
        an AI (Security Fix 002). A refused gate holds nothing back from the
        local record; it only keeps it off GitHub."""
        cfg = self.cfg
        if cfg["publish"] == "none":
            return {"published": False}
        from scan_secrets import find_in_text
        problems = gitops.verify_publishable(
            cfg["repo_dir"], self.worktree, task["task_id"], task["contract_path"],
            f"{cfg['remote']}/{cfg['base_branch']}", cfg["code_dir"], find_in_text)
        if problems:
            self.store.event("publish_gate_refused", self.id, task["task_id"], detail=problems)
            return {"published": False, "gate_refused": problems[:5]}
        head = gitops.rev(cfg["repo_dir"], f"refs/heads/{gitops.TASK_BRANCH.format(task['task_id'])}")
        allowed, why = publish_allowed(cfg, self.store)
        if not allowed:  # verified, but credentials could reach an AI: keep it local; republish once safe
            self.store.event("publish_blocked", self.id, task["task_id"], detail=why)
            return {"published": False, "publish_blocked": why, "gate_sha": head}
        title, body = gitops.pr_text(task["task_id"], contract)
        try:
            result = gitops.publish(cfg["repo_dir"], cfg["publish"], cfg["remote"], cfg["base_branch"], task,
                                    title, body, github_repo=cfg["github_repo"])
            result["gate_sha"] = head
            return result
        except Exception as exc:
            self.log.warning("publish failed for %s: %s", task["task_id"], exc)
            return {"published": False, "publish_error": str(exc)[:300]}


def main(cfg, worker_id):
    return Worker(cfg, worker_id).run()
