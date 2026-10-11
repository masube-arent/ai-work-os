"""Controller process: keeps N workers alive, syncs tasks from GitHub, reclaims
abandoned leases and tracks which agent CLIs are usable.

  python3 -m controller run          --config FILE   # supervise workers until SIGTERM
  python3 -m controller sync-once    --config FILE   # fetch and enqueue, no workers
  python3 -m controller status       --config FILE [--json]
  python3 -m controller check-config --config FILE
"""
import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import config as config_mod
from . import gitops, stages
from .procs import cmdline, kill_tree
from .store import Store, pid_alive

log = logging.getLogger("controller")


def prepare(cfg):
    state = Path(cfg["state_dir"])
    for sub in ("logs/stages", "worktrees", "workspaces"):
        (state / sub).mkdir(parents=True, exist_ok=True)
    store = Store(str(state / "aiwos.db"))
    store.init(cfg["workers"], cfg["model_concurrency"])
    return store


# ------------------------------------------------------------------ sync

def sync(cfg, store):
    """Enqueue contracts found on <remote>/<base>. A task branch on the remote,
    if one exists, is newer than main and wins, which is also how the queue is
    rebuilt after its database is lost."""
    repo, remote = cfg["repo_dir"], cfg["remote"]
    base = f"{remote}/{cfg['base_branch']}"
    gitops.fetch(repo, remote)
    seen, added = {}, []
    for path in gitops.list_contracts(repo, base, cfg["task_globs"]):
        try:
            contract = gitops.read_json_at(repo, base, path)
            task_id = contract["task_id"]
        except (ValueError, KeyError, gitops.GitError) as exc:
            store.event("sync_skipped", detail={"path": path, "error": str(exc)[:200]})
            continue
        if task_id in seen:
            store.event("sync_duplicate", task_id=task_id, detail={"path": path, "first": seen[task_id]})
            continue
        seen[task_id] = path
        ref = base
        branch_ref = f"{remote}/{gitops.TASK_BRANCH.format(task_id)}"
        if gitops.ref_exists(repo, branch_ref):
            try:
                contract, ref = gitops.read_json_at(repo, branch_ref, path), branch_ref
            except (ValueError, gitops.GitError):
                pass
        if contract.get("status") == "draft":
            continue
        reader = lambda p, r=ref: gitops.git(repo, "show", f"{r}:{p}")  # noqa: E731
        stage, agent, state, reason = stages.next_stage(contract, reader)
        if store.upsert_new(task_id, path, gitops.TASK_BRANCH.format(task_id), stage or "none", agent, state, reason):
            added.append(task_id)
    store.event("synced", detail={"contracts": len(seen), "added": added})
    republish(cfg, store)
    return added


def republish(cfg, store):
    """Retry pushes that failed: a task branch with commits the remote lacks.
    Only for tasks no worker holds, so a push never races a stage."""
    if cfg["publish"] == "none":
        return
    rows = store.db.execute("SELECT task_id, contract_path FROM tasks WHERE state != 'leased'").fetchall()
    for row in rows:
        if not gitops.unpublished(cfg["repo_dir"], cfg["remote"], row["task_id"]):
            continue
        branch = gitops.TASK_BRANCH.format(row["task_id"])
        try:
            contract = gitops.read_json_at(cfg["repo_dir"], f"refs/heads/{branch}", row["contract_path"])
            title, body = gitops.pr_text(row["task_id"], contract)
            result = gitops.publish(cfg["repo_dir"], cfg["publish"], cfg["remote"], cfg["base_branch"],
                                    {"task_id": row["task_id"]}, title, body)
            store.event("republished", task_id=row["task_id"], detail=result)
        except Exception as exc:
            store.event("republish_failed", task_id=row["task_id"], detail=str(exc)[:300])


def refresh_agents(cfg, store):
    """Ask the runner which agent CLIs are logged in (no model call)."""
    stale = [a for a in stages.RUNNABLE_AGENTS
             if not (s := store.agent_status(a)) or not s["checked_at"]
             or time.time() - s["checked_at"] > (cfg["agent_status_ttl_s"] if s["usable"] else cfg["auth_recheck_s"])]
    if not stale:
        return
    from .worker import stage_env
    env = stage_env(cfg, cfg["repo_dir"], 0)
    out = subprocess.run([sys.executable, str(Path(cfg["code_dir"]) / "scripts" / "aiwos.py"), "auth"],
                         capture_output=True, text=True, env=env, timeout=120)
    try:
        rows = json.loads(out.stdout)
    except ValueError:
        log.warning("aiwos auth produced no JSON: %s", out.stderr[-300:])
        return
    for row in rows:
        if row["agent"] in stale:
            was = store.agent_status(row["agent"])
            store.set_agent_status(row["agent"], row["usable"], row["reason"])
            if row["usable"] and was and not was["usable"]:
                with store.tx() as db:  # login fixed: release tasks that were parked on it
                    db.execute("UPDATE tasks SET state='queued', not_before=NULL, reason=NULL "
                               "WHERE state='waiting' AND agent=?", (row["agent"],))


# ------------------------------------------------------------------ supervision

class Controller:
    def __init__(self, cfg, config_path):
        self.cfg, self.config_path = cfg, str(Path(config_path).resolve())
        self.store = prepare(cfg)
        self.procs = {}          # worker_id -> Popen
        self.next_spawn = {}     # worker_id -> earliest respawn time
        self.stopping = False

    def stop_leftover_workers(self):
        """Workers of a controller that died without stopping them: end them
        before anything else, so no task is ever run by two workers."""
        for w in self.store.snapshot()["workers"]:
            pid = w["pid"]
            if pid and pid_alive(pid) and "controller" in cmdline(pid) and "worker" in cmdline(pid):
                log.warning("stopping leftover worker %s (pid %s)", w["worker_id"], pid)
                kill_tree(pid, grace=self.cfg["kill_grace_s"])
            self.store.worker_exited(w["worker_id"], restarted=False)

    def spawn(self, worker_id):
        logf = open(Path(self.cfg["state_dir"]) / "logs" / f"worker-{worker_id:02d}.stderr", "ab")
        proc = subprocess.Popen([sys.executable, "-m", "controller", "worker", "--config", self.config_path,
                                 "--id", str(worker_id)], cwd=str(Path(__file__).resolve().parent.parent),
                                stdout=logf, stderr=subprocess.STDOUT)
        logf.close()
        self.procs[worker_id] = proc
        self.store.event("worker_spawned", worker=worker_id, detail={"pid": proc.pid})

    def supervise_workers(self):
        now = time.time()
        for wid in range(1, self.cfg["workers"] + 1):
            proc = self.procs.get(wid)
            if proc is not None and proc.poll() is None:
                continue
            if proc is not None:
                restarts = self.store.db.execute("SELECT restarts FROM workers WHERE worker_id=?",
                                                 (wid,)).fetchone()["restarts"]
                self.store.worker_exited(wid, restarted=True)
                self.store.event("worker_exited", worker=wid, detail={"code": proc.returncode})
                self.next_spawn[wid] = now + min(60, 2 ** min(restarts, 6))  # crash-loop backoff
                self.procs[wid] = None
            if now >= self.next_spawn.get(wid, 0):
                self.spawn(wid)

    def housekeeping(self):
        for lease in self.store.dead_leases():
            child = lease["lease_child"]
            if child and pid_alive(child):
                log.warning("killing orphaned stage process %s of task %s", child, lease["task_id"])
                kill_tree(child, grace=self.cfg["kill_grace_s"])
                self.store.event("orphan_killed", task_id=lease["task_id"], detail={"pid": child})
            worktree = Path(self.cfg["state_dir"]) / "worktrees" / f"w{lease['lease_owner']:02d}"
            if worktree.exists():
                try:
                    gitops.reset_worktree(str(worktree))
                except gitops.GitError as exc:
                    log.warning("could not reset %s: %s", worktree, exc)
        self.store.reclaim(self.cfg["max_stage_attempts"])

    def run(self):
        signal.signal(signal.SIGTERM, self.on_signal)
        signal.signal(signal.SIGINT, self.on_signal)
        self.stop_leftover_workers()
        self.housekeeping()
        base_ref = f"{self.cfg['remote']}/{self.cfg['base_branch']}"
        if not gitops.ref_exists(self.cfg["repo_dir"], base_ref):
            gitops.fetch(self.cfg["repo_dir"], self.cfg["remote"])
        for wid in range(1, self.cfg["workers"] + 1):  # sequentially: git worktree add is not concurrency-safe
            gitops.ensure_worktree(self.cfg["repo_dir"],
                                   str(Path(self.cfg["state_dir"]) / "worktrees" / f"w{wid:02d}"), base_ref)
        self.store.event("controller_started", detail={"pid": os.getpid(), "workers": self.cfg["workers"],
                                                       "model_concurrency": self.cfg["model_concurrency"]})
        last_sync = 0.0
        while not self.stopping:
            try:
                if time.time() - last_sync >= self.cfg["sync_interval_s"]:
                    last_sync = time.time()
                    sync(self.cfg, self.store)
            except Exception as exc:  # GitHub unreachable etc.: keep the workers going
                log.warning("sync failed: %s", exc)
                self.store.event("sync_failed", detail=str(exc)[:300])
            try:
                refresh_agents(self.cfg, self.store)
            except Exception as exc:
                log.warning("agent check failed: %s", exc)
            self.housekeeping()
            self.supervise_workers()
            end = time.time() + min(self.cfg["poll_interval_s"], 5)
            while time.time() < end and not self.stopping:
                time.sleep(0.2)
        self.shutdown()
        return 0

    def on_signal(self, signum, frame):
        self.stopping = True

    def shutdown(self):
        log.info("stopping workers")
        for proc in self.procs.values():
            if proc and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        deadline = time.time() + self.cfg["kill_grace_s"] + 30
        while time.time() < deadline and any(p and p.poll() is None for p in self.procs.values()):
            time.sleep(0.2)
        for wid, proc in self.procs.items():
            if proc and proc.poll() is None:
                kill_tree(proc.pid, grace=5)
            self.store.worker_exited(wid, restarted=False)
        self.housekeeping()
        self.store.event("controller_stopped")


# ------------------------------------------------------------------ status

def status(cfg, as_json):
    snap = prepare(cfg).snapshot()
    if as_json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return 0
    now = time.time()
    print(f"workers ({len(snap['workers'])}):")
    for w in snap["workers"]:
        age = f"{now - w['heartbeat']:.0f}s" if w["heartbeat"] else "—"
        print(f"  w{w['worker_id']:02d} {w['state'] or '—':<8} pid={w['pid'] or '—':<8} heartbeat={age:<6} "
              f"restarts={w['restarts']} task={w['task_id'] or '—'} stage={w['stage'] or '—'}")
    busy = sum(1 for s in snap["slots"] if s["holder"])
    print(f"model slots: {busy}/{len(snap['slots'])} in use")
    for a in snap["agents"]:
        until = a["backoff_until"]
        print(f"  agent {a['agent']}: usable={bool(a['usable'])} reason={a['reason'] or '—'} "
              f"backoff={'until ' + time.strftime('%H:%M:%S', time.localtime(until)) if until and until > now else '—'}")
    print(f"tasks ({len(snap['tasks'])}):")
    for t in snap["tasks"]:
        print(f"  {t['task_id']:<12} {t['state']:<8} stage={t['stage']:<7} agent={t['agent'] or '—':<12} "
              f"ai_runs={t['ai_runs']} {t['reason'] or ''}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m controller")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "sync-once", "status", "check-config", "worker"):
        s = sub.add_parser(name)
        s.add_argument("--config", required=True)
        if name == "status":
            s.add_argument("--json", action="store_true")
        if name == "worker":
            s.add_argument("--id", type=int, required=True)
    args = p.parse_args(argv)
    cfg = config_mod.load(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.cmd == "check-config":
        print(json.dumps(cfg, indent=2))
        return 0
    if args.cmd == "status":
        return status(cfg, args.json)
    if args.cmd == "sync-once":
        store = prepare(cfg)
        refresh_agents(cfg, store)
        print(json.dumps({"added": sync(cfg, store)}))
        return status(cfg, False)
    if args.cmd == "worker":
        from .worker import main as worker_main
        return worker_main(cfg, args.id)
    return Controller(cfg, args.config).run()
