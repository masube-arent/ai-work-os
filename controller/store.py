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

    def init(self, workers, model_slots):
        self.db.executescript(SCHEMA)
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

    def upsert_new(self, task_id, contract_path, branch, stage, agent, state, reason=None):
        """Insert a task the queue has not seen. Existing rows are left alone:
        once a task is in the queue, workers own its progress."""
        with self.tx() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO tasks(task_id, contract_path, branch, stage, agent, state, reason, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)", (task_id, contract_path, branch, stage, agent, state, reason, time.time()))
            if cur.rowcount:
                self.event("enqueued", task_id=task_id, stage=stage, detail={"state": state, "reason": reason}, db=db)
            return bool(cur.rowcount)

    def claim(self, worker_id, pid, ttl, usable_agents, model_slots_needed=True):
        """Atomically lease the oldest runnable task. A stage that starts a
        model also takes a model slot in the same transaction, or is skipped."""
        now = time.time()
        with self.tx() as db:
            rows = db.execute(
                "SELECT * FROM tasks WHERE state='queued' AND (not_before IS NULL OR not_before <= ?) "
                "ORDER BY updated_at, task_id", (now,)).fetchall()
            for row in rows:
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

    def dead_leases(self):
        """Leases whose worker process is gone. Before such a lease is handed
        on, its stage process must be killed (or the task runs twice) and the
        dead worker's worktree detached (or git refuses the branch elsewhere)."""
        return [dict(r) for r in self.db.execute(
            "SELECT task_id, lease_owner, lease_pid, lease_child FROM tasks WHERE state='leased'")
            if not pid_alive(r["lease_pid"])]

    def finish(self, worker_id, pid, task_id, *, stage, agent, state, reason=None, ai_run=False,
               delay=0.0, reset_attempts=True, detail=None):
        """Release a lease and record what the task needs next. Only the lease
        holder may do this; a worker whose lease was reclaimed gets False."""
        now = time.time()
        with self.tx() as db:
            cur = db.execute(
                "UPDATE tasks SET state=?, stage=?, agent=?, reason=?, lease_owner=NULL, lease_pid=NULL, "
                "lease_expires=NULL, lease_child=NULL, not_before=?, ai_runs=ai_runs+?, attempts=CASE WHEN ? THEN 0 ELSE attempts END, "
                "updated_at=? WHERE task_id=? AND state='leased' AND lease_owner=? AND lease_pid=?",
                (state, stage, agent, reason, now + delay if delay else None, 1 if ai_run else 0,
                 1 if reset_attempts else 0, now, task_id, worker_id, pid))
            db.execute("UPDATE model_slots SET holder=NULL, pid=NULL, acquired=NULL WHERE holder=?", (worker_id,))
            db.execute("UPDATE workers SET state='idle', task_id=NULL, stage=NULL, heartbeat=? WHERE worker_id=?",
                       (now, worker_id))
            ok = cur.rowcount == 1
            self.event("released" if ok else "release_lost", worker_id, task_id, stage,
                       {"state": state, "reason": reason, **(detail or {})}, db=db)
            return ok

    def reclaim(self, max_attempts, everything=False):
        """Return expired or orphaned leases to the queue (or to a human after
        too many attempts). `everything` is used at controller start, when no
        worker of a previous controller can still be running."""
        now = time.time()
        reclaimed = []
        with self.tx() as db:
            for row in db.execute("SELECT * FROM tasks WHERE state='leased'").fetchall():
                if not (everything or row["lease_expires"] < now or not pid_alive(row["lease_pid"])):
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
                if everything or not pid_alive(slot["pid"]):
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
