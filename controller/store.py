"""SQLite (WAL) queue shared by the controller and its worker processes.

Every decision that two workers could race on (claiming a task, taking a model
slot, reclaiming a lease) happens inside one BEGIN IMMEDIATE transaction, so
SQLite's single-writer lock makes it atomic across processes.

Task states:
  queued   ready for a worker once not_before has passed
  leased   a worker holds it; lease_expires is renewed by heartbeat
  waiting  blocked on something that clears by itself (agent login, backoff)
  held     needs a human (passed, escalated, budget spent, repeated crash)
  done     accepted or cancelled
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  contract_path TEXT NOT NULL,
  branch TEXT NOT NULL,
  stage TEXT NOT NULL,
  agent TEXT,
  state TEXT NOT NULL CHECK (state IN ('queued','leased','waiting','held','done')),
  lease_owner INTEGER, lease_pid INTEGER, lease_expires REAL, lease_child INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0,
  ai_runs INTEGER NOT NULL DEFAULT 0,
  not_before REAL,
  reason TEXT,
  main_sha TEXT,      -- blob id of the contract on the remote base branch at the last sync
  remote_sha TEXT,    -- task branch commit last seen on (or pushed to) the remote
  pr_url TEXT,        -- set once a pull request is known to exist
  pr_error TEXT,      -- last PR creation failure, retried by the sync
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workers (
  worker_id INTEGER PRIMARY KEY,
  pid INTEGER, state TEXT, task_id TEXT, stage TEXT,
  heartbeat REAL, started_at REAL, restarts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS model_slots (
  slot_id INTEGER PRIMARY KEY, holder INTEGER, pid INTEGER, acquired REAL
);
CREATE TABLE IF NOT EXISTS agents (
  agent TEXT PRIMARY KEY,
  usable INTEGER, reason TEXT, checked_at REAL,
  backoff_until REAL, backoff_level INTEGER NOT NULL DEFAULT 0, backoff_reason TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, worker INTEGER, task_id TEXT, stage TEXT, kind TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id, kind);
"""

MODEL_STAGES = {"work", "review"}


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie child still answers kill(0); treat it as gone
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


class Store:
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("PRAGMA synchronous=NORMAL")

    # columns added after the first v0.3 schema; added to older databases on start
    MIGRATIONS = {"main_sha": "TEXT", "remote_sha": "TEXT", "pr_url": "TEXT", "pr_error": "TEXT"}

    def init(self, workers, model_slots):
        self.db.executescript(SCHEMA)
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(tasks)")}
        for column, kind in self.MIGRATIONS.items():
            if column not in have:
                self.db.execute(f"ALTER TABLE tasks ADD COLUMN {column} {kind}")
        with self.tx() as db:
            db.execute("DELETE FROM workers WHERE worker_id > ?", (workers,))
            for i in range(1, workers + 1):
                db.execute("INSERT OR IGNORE INTO workers(worker_id, state, restarts) VALUES (?, 'stopped', 0)", (i,))
            db.execute("DELETE FROM model_slots WHERE slot_id > ? AND holder IS NULL", (model_slots,))
            for i in range(1, model_slots + 1):
                db.execute("INSERT OR IGNORE INTO model_slots(slot_id) VALUES (?)", (i,))

    @contextmanager
    def tx(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def event(self, kind, worker=None, task_id=None, stage=None, detail=None, db=None):
        (db or self.db).execute(
            "INSERT INTO events(ts, worker, task_id, stage, kind, detail) VALUES (?,?,?,?,?,?)",
            (time.time(), worker, task_id, stage, kind,
             detail if detail is None or isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)))

    # ------------------------------------------------------------------ tasks

    def upsert_new(self, task_id, contract_path, branch, stage, agent, state, reason=None, main_sha=None,
                   remote_sha=None):
        """Insert a task the queue has not seen. Changes to known tasks go
        through resync(), which never touches a leased task."""
        with self.tx() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO tasks(task_id, contract_path, branch, stage, agent, state, reason, main_sha, "
                "remote_sha, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (task_id, contract_path, branch, stage, agent, state, reason, main_sha, remote_sha, time.time()))
            if cur.rowcount:
                self.event("enqueued", task_id=task_id, stage=stage, detail={"state": state, "reason": reason}, db=db)
            return bool(cur.rowcount)

    def get(self, task_id):
        row = self.db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def resync(self, task_id, *, kind, detail=None, **fields):
        """Apply what the sync learned from GitHub to a task no worker holds.
        Returns False (and changes nothing) if a worker has leased it meanwhile."""
        allowed = {"stage", "agent", "state", "reason", "main_sha", "remote_sha", "not_before", "attempts"}
        assert set(fields) <= allowed, fields
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as db:
            cur = db.execute(f"UPDATE tasks SET {sets}, updated_at=? WHERE task_id=? AND state != 'leased'",
                             (*fields.values(), time.time(), task_id))
            if cur.rowcount:
                self.event(kind, task_id=task_id, stage=fields.get("stage"), detail=detail, db=db)
            return bool(cur.rowcount)

    def record_publish(self, task_id, result):
        """Remember what a publish achieved, independently of any lease."""
        fields = {}
        if result.get("pushed_sha"):
            fields["remote_sha"] = result["pushed_sha"]
        if result.get("pr"):
            fields.update(pr_url=result["pr"], pr_error=None)
        elif result.get("pr_error"):
            fields["pr_error"] = result["pr_error"]
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            with self.tx() as db:
                db.execute(f"UPDATE tasks SET {sets} WHERE task_id=?", (*fields.values(), task_id))

    def claim(self, worker_id, pid, ttl, usable_agents, model_slots_needed=True, max_attempts=None):
        """Atomically lease the oldest runnable task. A stage that starts a
        model also takes a model slot in the same transaction, or is skipped.
        A task that already used max_attempts on its stage goes to a human
        instead of being leased again."""
        now = time.time()
        with self.tx() as db:
            rows = db.execute(
                "SELECT * FROM tasks WHERE state='queued' AND (not_before IS NULL OR not_before <= ?) "
                "ORDER BY updated_at, task_id", (now,)).fetchall()
            for row in rows:
                if max_attempts and row["attempts"] >= max_attempts:
                    reason = (f"stage '{row['stage']}' held after {row['attempts']} attempts "
                              f"(max_stage_attempts={max_attempts}); last: {row['reason']}")
                    db.execute("UPDATE tasks SET state='held', reason=?, updated_at=? WHERE task_id=?",
                               (reason[:500], now, row["task_id"]))
                    self.event("attempts_exhausted", task_id=row["task_id"], stage=row["stage"], detail=reason, db=db)
                    continue
                if row["agent"] and row["agent"] not in usable_agents:
                    continue
                slot = None
                if row["stage"] in MODEL_STAGES and model_slots_needed:
                    free = db.execute("SELECT slot_id FROM model_slots WHERE holder IS NULL "
                                      "ORDER BY slot_id LIMIT 1").fetchone()
                    if free is None:
                        continue  # model capacity full: a non-model stage may still run
                    slot = free["slot_id"]
                    db.execute("UPDATE model_slots SET holder=?, pid=?, acquired=? WHERE slot_id=?",
                               (worker_id, pid, now, slot))
                db.execute("UPDATE tasks SET state='leased', lease_owner=?, lease_pid=?, lease_expires=?, "
                           "attempts=attempts+1, updated_at=? WHERE task_id=? AND state='queued'",
                           (worker_id, pid, now + ttl, now, row["task_id"]))
                db.execute("UPDATE workers SET state='busy', task_id=?, stage=?, heartbeat=? WHERE worker_id=?",
                           (row["task_id"], row["stage"], now, worker_id))
                self.event("claimed", worker_id, row["task_id"], row["stage"],
                           {"attempt": row["attempts"] + 1, "slot": slot}, db=db)
                task = dict(row)
                task["attempts"] += 1
                task["slot"] = slot
                return task
        return None

    def heartbeat(self, worker_id, pid, ttl, task_id=None):
        now = time.time()
        with self.tx() as db:
            db.execute("UPDATE workers SET heartbeat=?, pid=? WHERE worker_id=?", (now, pid, worker_id))
            if task_id:
                cur = db.execute("UPDATE tasks SET lease_expires=? WHERE task_id=? AND state='leased' "
                                 "AND lease_owner=? AND lease_pid=?", (now + ttl, task_id, worker_id, pid))
                return cur.rowcount == 1  # False: the lease was taken away
        return True

    def set_child(self, worker_id, pid, task_id, child_pid):
        with self.tx() as db:
            db.execute("UPDATE tasks SET lease_child=? WHERE task_id=? AND state='leased' AND lease_owner=? "
                       "AND lease_pid=?", (child_pid, task_id, worker_id, pid))

    def stale_leases(self):
        """Leases whose worker is gone or stopped renewing them (frozen, hung).
        The controller must stop the owner and its stage process, and detach the
        owner's worktree, before passing such a task to reclaim()."""
        now = time.time()
        return [dict(r) for r in self.db.execute(
            "SELECT task_id, lease_owner, lease_pid, lease_child, lease_expires FROM tasks WHERE state='leased'")
            if not pid_alive(r["lease_pid"]) or r["lease_expires"] < now]

    def finish(self, worker_id, pid, task_id, *, stage, agent, state, reason=None, ai_run=False,
               delay=0.0, reset_attempts=True, refund_attempt=False, max_attempts=None, detail=None):
        """Release a lease and record what the task needs next. Only the lease
        holder may do this; a worker whose lease was reclaimed gets False.

        reset_attempts: the stage made progress, so its attempt count starts over.
        refund_attempt: the attempt does not count (the controller was stopped).
        max_attempts:   a retry that has used them all is held for a human."""
        now = time.time()
        with self.tx() as db:
            row = db.execute("SELECT attempts FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            attempts = row["attempts"] if row else 0
            if refund_attempt:
                attempts = max(0, attempts - 1)
            elif reset_attempts:
                attempts = 0
            if state in ("queued", "waiting") and not reset_attempts and max_attempts and attempts >= max_attempts:
                state = "held"
                reason = (f"stage '{stage}' held after {attempts} attempts (max_stage_attempts={max_attempts}); "
                          f"last: {reason}")[:500]
                delay = 0
            cur = db.execute(
                "UPDATE tasks SET state=?, stage=?, agent=?, reason=?, lease_owner=NULL, lease_pid=NULL, "
                "lease_expires=NULL, lease_child=NULL, not_before=?, ai_runs=ai_runs+?, attempts=?, "
                "updated_at=? WHERE task_id=? AND state='leased' AND lease_owner=? AND lease_pid=?",
                (state, stage, agent, reason, now + delay if delay else None, 1 if ai_run else 0,
                 attempts, now, task_id, worker_id, pid))
            db.execute("UPDATE model_slots SET holder=NULL, pid=NULL, acquired=NULL WHERE holder=?", (worker_id,))
            db.execute("UPDATE workers SET state='idle', task_id=NULL, stage=NULL, heartbeat=? WHERE worker_id=?",
                       (now, worker_id))
            ok = cur.rowcount == 1
            self.event("released" if ok else "release_lost", worker_id, task_id, stage,
                       {"state": state, "reason": reason, **(detail or {})}, db=db)
            return ok

    def reclaim(self, max_attempts, confirmed=()):
        """Return leases to the queue (or to a human after too many attempts).
        Only leases in `confirmed` are touched: (task_id, lease_pid) pairs whose
        owner and stage process the controller has verified are gone. A lease
        whose owner is merely late is never handed on here."""
        now = time.time()
        reclaimed = []
        confirmed = set(confirmed)
        with self.tx() as db:
            for row in db.execute("SELECT * FROM tasks WHERE state='leased'").fetchall():
                if (row["task_id"], row["lease_pid"]) not in confirmed:
                    continue
                if row["attempts"] >= max_attempts:
                    state, reason = "held", f"stage '{row['stage']}' leased {row['attempts']} times without finishing"
                else:
                    state, reason = "queued", "lease reclaimed"
                db.execute("UPDATE tasks SET state=?, reason=?, lease_owner=NULL, lease_pid=NULL, lease_expires=NULL, "
                           "lease_child=NULL, updated_at=? WHERE task_id=?", (state, reason, now, row["task_id"]))
                self.event("reclaimed", row["lease_owner"], row["task_id"], row["stage"],
                           {"to": state, "pid": row["lease_pid"]}, db=db)
                reclaimed.append(row["task_id"])
            for slot in db.execute("SELECT * FROM model_slots WHERE holder IS NOT NULL").fetchall():
                if not pid_alive(slot["pid"]):
                    db.execute("UPDATE model_slots SET holder=NULL, pid=NULL, acquired=NULL WHERE slot_id=?",
                               (slot["slot_id"],))
                    self.event("slot_freed", slot["holder"], detail={"slot": slot["slot_id"]}, db=db)
            # tasks waiting on something that has cleared go back to the queue
            db.execute("UPDATE tasks SET state='queued', reason=NULL, updated_at=? "
                       "WHERE state='waiting' AND not_before IS NOT NULL AND not_before <= ?", (now, now))
        return reclaimed

    # ------------------------------------------------------------------ agents

    def agent_status(self, agent):
        row = self.db.execute("SELECT * FROM agents WHERE agent=?", (agent,)).fetchone()
        return dict(row) if row else None

    def set_agent_status(self, agent, usable, reason):
        with self.tx() as db:
            db.execute("INSERT INTO agents(agent, usable, reason, checked_at) VALUES (?,?,?,?) "
                       "ON CONFLICT(agent) DO UPDATE SET usable=excluded.usable, reason=excluded.reason, "
                       "checked_at=excluded.checked_at", (agent, int(usable), reason, time.time()))

    def backoff(self, agent, initial, maximum, reason):
        with self.tx() as db:
            row = db.execute("SELECT backoff_level FROM agents WHERE agent=?", (agent,)).fetchone()
            level = (row["backoff_level"] if row else 0) + 1
            wait = min(maximum, initial * 2 ** (level - 1))
            db.execute("INSERT INTO agents(agent, backoff_until, backoff_level, backoff_reason) VALUES (?,?,?,?) "
                       "ON CONFLICT(agent) DO UPDATE SET backoff_until=excluded.backoff_until, "
                       "backoff_level=excluded.backoff_level, backoff_reason=excluded.backoff_reason",
                       (agent, time.time() + wait, level, reason))
            self.event("backoff", detail={"agent": agent, "seconds": wait, "level": level, "reason": reason}, db=db)
            return wait

    def clear_backoff(self, agent):
        with self.tx() as db:
            db.execute("UPDATE agents SET backoff_level=0, backoff_until=NULL, backoff_reason=NULL WHERE agent=?",
                       (agent,))

    def usable_agents(self, now=None):
        now = now or time.time()
        return {r["agent"] for r in self.db.execute("SELECT * FROM agents").fetchall()
                if r["usable"] and not (r["backoff_until"] and r["backoff_until"] > now)}

    # ------------------------------------------------------------------ workers

    def worker_started(self, worker_id, pid):
        with self.tx() as db:
            db.execute("UPDATE workers SET pid=?, state='idle', task_id=NULL, stage=NULL, heartbeat=?, started_at=? "
                       "WHERE worker_id=?", (pid, time.time(), time.time(), worker_id))

    def worker_exited(self, worker_id, restarted):
        with self.tx() as db:
            db.execute("UPDATE workers SET state='stopped', pid=NULL, restarts=restarts+? WHERE worker_id=?",
                       (1 if restarted else 0, worker_id))

    def snapshot(self):
        q = lambda sql, *a: [dict(r) for r in self.db.execute(sql, a).fetchall()]  # noqa: E731
        return {
            "workers": q("SELECT * FROM workers ORDER BY worker_id"),
            "tasks": q("SELECT task_id, stage, agent, state, attempts, ai_runs, reason, lease_owner FROM tasks "
                       "ORDER BY task_id"),
            "slots": q("SELECT * FROM model_slots ORDER BY slot_id"),
            "agents": q("SELECT * FROM agents ORDER BY agent"),
        }
