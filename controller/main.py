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
from .procs import cmdline, find_tagged, kill_tree
from .publishing import ISOLATION_KEY, check_isolation, publish_allowed
from .store import Store, pid_alive

log = logging.getLogger("controller")


def prepare(cfg):
    state = Path(cfg["state_dir"])
    for sub in ("logs/stages", "worktrees"):
        (state / sub).mkdir(parents=True, exist_ok=True)
    os.chmod(state, 0o700)  # queue, logs and worktrees: trusted side only
    workspaces = Path(cfg["workspaces_dir"])
    workspaces.mkdir(parents=True, exist_ok=True)
    if cfg.get("agent_user"):  # the agent may enter (not list) the workspaces root
        import grp
        os.chown(workspaces, -1, grp.getgrnam(cfg["agent_group"]).gr_gid)
        os.chmod(workspaces, 0o710)
    store = Store(str(state / "aiwos.db"))
    store.init(cfg["workers"], cfg["model_concurrency"])
    return store


# ------------------------------------------------------------------ sync

def next_stage_at(repo, ref, path):
    contract = gitops.read_json_at(repo, ref, path)
    return contract, stages.next_stage(contract, lambda p: gitops.git(repo, "show", f"{ref}:{p}"))


def sync(cfg, store):
    """Bring the queue in line with GitHub: enqueue new contracts from
    <remote>/<base> and reconcile known tasks no worker holds. A task branch on
    the remote is newer than main for that task, which is also how the queue
    is rebuilt after its database is lost."""
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
        branch = gitops.TASK_BRANCH.format(task_id)
        main_sha = gitops.rev(repo, f"{base}:{path}")
        remote_sha = gitops.rev(repo, f"refs/remotes/{remote}/{branch}")
        row = store.get(task_id)
        if row is not None:
            if row["state"] != "leased":
                reconcile(cfg, store, row, path, main_sha, remote_sha)
            continue
        ref = f"{remote}/{branch}" if remote_sha else base
        try:
            contract, (stage, agent, state, reason) = next_stage_at(repo, ref, path)
        except (ValueError, gitops.GitError):
            ref = base
            contract, (stage, agent, state, reason) = next_stage_at(repo, ref, path)
        if contract.get("status") == "draft":
            continue
        if store.upsert_new(task_id, path, branch, stage or "none", agent, state, reason,
                            main_sha=main_sha, remote_sha=remote_sha):
            added.append(task_id)
    store.event("synced", detail={"contracts": len(seen), "added": added})
    republish(cfg, store)
    return added


def reconcile(cfg, store, row, path, main_sha, remote_sha):
    """Take in what changed on GitHub for a task no worker holds. A change that
    only adds to what the controller has (a person's commit on top of the task
    branch, a new contract for a task not yet started) is taken in. A change
    that would overwrite or contradict work already done is held for a person;
    nothing is overwritten automatically."""
    repo, remote = cfg["repo_dir"], cfg["remote"]
    task_id = row["task_id"]
    branch = gitops.TASK_BRANCH.format(task_id)
    local = gitops.rev(repo, f"refs/heads/{branch}")
    started = bool(local or remote_sha)

    # 1. the contract on main
    if row["main_sha"] is None:  # queue created before this field existed: record, do not act
        store.resync(task_id, kind="sync_baseline", main_sha=main_sha)
    elif main_sha != row["main_sha"]:
        if not started:
            _, (stage, agent, state, reason) = next_stage_at(repo, f"{remote}/{cfg['base_branch']}", path)
            store.resync(task_id, kind="main_contract_taken_in", detail={"from": row["main_sha"], "to": main_sha},
                         stage=stage or "none", agent=agent, state=state, reason=reason, main_sha=main_sha,
                         attempts=0, not_before=None)
        else:
            store.resync(task_id, kind="main_contract_changed", detail={"from": row["main_sha"], "to": main_sha},
                         state="held", main_sha=main_sha,
                         reason="contract changed on main after work started; a person must decide "
                                "(the task branch keeps the contract the work was done against)")
        return

    # 2. the task branch on the remote
    if not remote_sha or remote_sha == row["remote_sha"]:
        return
    if local and local != remote_sha:
        if gitops.is_ancestor(repo, remote_sha, local):
            return  # we are ahead (unpublished results): republish pushes them
        if not gitops.is_ancestor(repo, local, remote_sha):
            store.resync(task_id, kind="task_branch_diverged", state="held", remote_sha=remote_sha,
                         detail={"local": local, "remote": remote_sha},
                         reason=f"diverged from GitHub: local {local[:12]} and {remote} {remote_sha[:12]} each "
                                "have commits the other lacks; a person must reconcile (nothing was overwritten)")
            return
        # GitHub is ahead: fast-forward, and only if our branch is still where we saw it
        gitops.git(repo, "update-ref", f"refs/heads/{branch}", remote_sha, local)
    _, (stage, agent, state, reason) = next_stage_at(repo, f"refs/remotes/{remote}/{branch}", path)
    store.resync(task_id, kind="task_branch_taken_in", detail={"from": row["remote_sha"], "to": remote_sha},
                 stage=stage or "none", agent=agent, state=state, reason=reason, remote_sha=remote_sha,
                 attempts=0, not_before=None)


def republish(cfg, store):
    """Retry what a worker could not finish publishing, for tasks no worker
    holds: a push that failed, or (publish=github) a pull request whose creation
    failed after its push succeeded. ensure_pull_request looks for an open PR
    first, so a retry never creates a second one."""
    if cfg["publish"] == "none":
        return
    allowed, why = publish_allowed(cfg, store)
    if not allowed:
        return  # fail closed; the worker already recorded publish_blocked for each task
    repo, remote = cfg["repo_dir"], cfg["remote"]
    rows = store.db.execute("SELECT * FROM tasks WHERE state != 'leased'").fetchall()
    for row in rows:
        task_id = row["task_id"]
        if (row["reason"] or "").startswith("diverged"):
            continue
        branch = gitops.TASK_BRANCH.format(task_id)
        try:
            if gitops.unpublished(repo, remote, task_id):
                head = gitops.rev(repo, f"refs/heads/{branch}")
                if head != row["gate_sha"]:  # only commits that passed the publish gate leave the machine
                    problems = gate_in_scratch_worktree(cfg, task_id, row["contract_path"])
                    if problems:
                        store.event("publish_gate_refused", task_id=task_id, detail=problems[:5])
                        continue
                    store.record_publish(task_id, {"gate_sha": head})
                contract = gitops.read_json_at(repo, f"refs/heads/{branch}", row["contract_path"])
                title, body = gitops.pr_text(task_id, contract)
                result = gitops.publish(repo, cfg["publish"], remote, cfg["base_branch"], {"task_id": task_id},
                                        title, body, github_repo=cfg["github_repo"])
            elif cfg["publish"] == "github" and not row["pr_url"] and gitops.ref_exists(
                    repo, f"refs/remotes/{remote}/{branch}"):
                contract = gitops.read_json_at(repo, f"refs/remotes/{remote}/{branch}", row["contract_path"])
                title, body = gitops.pr_text(task_id, contract)
                try:
                    result = {"pr": gitops.ensure_pull_request(repo, cfg["base_branch"], branch, title, body,
                                                               github_repo=cfg["github_repo"])}
                except (gitops.GitError, OSError, subprocess.TimeoutExpired) as exc:
                    result = {"pr_error": str(exc)[:300]}
            else:
                continue
            store.record_publish(task_id, result)
            store.event("republished", task_id=task_id, detail=result)
        except Exception as exc:
            store.event("republish_failed", task_id=task_id, detail=str(exc)[:300])


def export_isolation_env(cfg):
    """The controller's own calls into scripts/isolation.py (the probe) read
    the agent user from the environment, as the runner's do."""
    if cfg.get("agent_user"):
        os.environ.update(AIWOS_AGENT_USER=cfg["agent_user"], AIWOS_AGENT_GROUP=cfg["agent_group"],
                          AIWOS_AGENT_PYTHON=cfg["agent_python"])


def refresh_isolation(cfg, store):
    if not cfg.get("agent_user"):
        verdict, _ = store.get_meta(ISOLATION_KEY)
        if verdict is None or verdict.get("ok"):
            store.set_meta(ISOLATION_KEY, {"ok": False, "reason": "agent_user not configured: AI runs as the "
                                           "trusted user"})
        return False
    return check_isolation(cfg, store)[0]


def gate_in_scratch_worktree(cfg, task_id, contract_path):
    """Run the publish gate for a task branch no worker has checked out (commits
    made while publishing was off, or recorded before the gate existed)."""
    from scan_secrets import find_in_text
    repo = cfg["repo_dir"]
    scratch = Path(cfg["state_dir"]) / "worktrees" / "gate"
    gitops.git(repo, "worktree", "prune")
    if scratch.exists():
        gitops.git(repo, "worktree", "remove", "--force", str(scratch), check=False)
    gitops.git(repo, "worktree", "add", "--detach", "--force", str(scratch), f"refs/heads/{gitops.TASK_BRANCH.format(task_id)}")
    try:
        return gitops.verify_publishable(repo, scratch, task_id, contract_path,
                                         f"{cfg['remote']}/{cfg['base_branch']}", cfg["code_dir"], find_in_text)
    finally:
        gitops.git(repo, "worktree", "remove", "--force", str(scratch), check=False)


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
        export_isolation_env(cfg)
        self.last_isolation_check = 0.0
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
        """Hand on leases whose owner died or stopped renewing, in this order:
        stop the owner (it may be alive but frozen), stop its stage process,
        confirm both are gone, detach the owner's worktree, and only then
        return the task to the queue. A lease not confirmed this round is
        left for the next one."""
        confirmed = []
        for lease in self.store.stale_leases():
            owner, child = lease["lease_pid"], lease["lease_child"]
            ours = pid_alive(owner) and "controller" in cmdline(owner) and "worker" in cmdline(owner)
            if ours:
                log.warning("worker %s (pid %s) stopped renewing its lease on %s; stopping it",
                            lease["lease_owner"], owner, lease["task_id"])
                kill_tree(owner, grace=self.cfg["kill_grace_s"])
                self.store.event("stale_owner_stopped", worker=lease["lease_owner"], task_id=lease["task_id"],
                                 detail={"pid": owner})
            # the recorded child, plus anything carrying this lease's tag: the worker
            # may have died after starting the stage but before recording its pid
            stage_procs = set(find_tagged(f"{lease['task_id']}:{owner}"))
            if child and pid_alive(child):
                stage_procs.add(child)
            for proc in stage_procs:
                log.warning("killing orphaned stage process %s of task %s", proc, lease["task_id"])
                kill_tree(proc, grace=self.cfg["kill_grace_s"])
                self.store.event("orphan_killed", task_id=lease["task_id"], detail={"pid": proc})
            if (ours and pid_alive(owner)) or any(pid_alive(p) for p in stage_procs) or \
                    find_tagged(f"{lease['task_id']}:{owner}"):
                continue  # not confirmed gone: never hand the task on beside a running process
            worktree = Path(self.cfg["state_dir"]) / "worktrees" / f"w{lease['lease_owner']:02d}"
            if worktree.exists():
                try:
                    gitops.reset_worktree(str(worktree))
                except gitops.GitError as exc:
                    log.warning("could not reset %s: %s", worktree, exc)
                    continue
            confirmed.append((lease["task_id"], owner))
        self.store.reclaim(self.cfg["max_stage_attempts"], confirmed)

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
        try:  # before any worker can publish
            refresh_isolation(self.cfg, self.store)
        except Exception as exc:
            self.store.set_meta(ISOLATION_KEY, {"ok": False, "reason": f"probe error: {exc}"[:300]})
        self.last_isolation_check = time.time()
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
            if time.time() - self.last_isolation_check >= self.cfg["isolation_check_ttl_s"] / 2:
                self.last_isolation_check = time.time()
                try:
                    refresh_isolation(self.cfg, self.store)
                except Exception as exc:  # a probe that cannot run is a failed probe
                    self.store.set_meta(ISOLATION_KEY, {"ok": False, "reason": f"probe error: {exc}"[:300]})
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
