"""v0.3 controller tests with stub agents (no model is called).

Each test builds a sandbox: a bare git repository standing in for GitHub,
synthetic contracts under tasks/, a controller state directory, and stub
`claude` / `codex` executables. See tests/controller_harness.py.

Run: python3 -m unittest tests.test_controller -v
"""
import json
import multiprocessing
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from controller_harness import ROOT, Sandbox, git, max_overlap  # noqa: E402

sys.path.insert(0, str(ROOT))
from controller.store import Store, pid_alive  # noqa: E402
from controller import config as config_mod  # noqa: E402
from controller.worker import stage_env  # noqa: E402

RUN_TASK = re.compile(r"(T-\d{4})-(worker|reviewer)-")


def all_settled(sb, n):
    tasks = sb.tasks()
    return len(tasks) == n and all(t["state"] in ("held", "done") for t in tasks.values())


def completed_worker_runs(contract, sb):
    """Completed worker run records cited by a pushed contract."""
    count = 0
    for ev in contract["evidence"]:
        if ev["kind"] == "run_record" and "-worker-" in ev["path"]:
            rec = json.loads(subprocess.run(["git", "--git-dir", str(sb.remote), "show",
                                             f"aiwos/task/{contract['task_id']}:{ev['path']}"],
                                            capture_output=True, text=True, check=True).stdout)
            count += rec["status"] == "completed"
    return count


def calls_by_task(sb):
    out = {}
    for c in sb.spy_calls():
        m = RUN_TASK.search(c["cwd"])
        if m:
            out.setdefault((m.group(1), m.group(2)), []).append(c)
    return out


class ControllerCase(unittest.TestCase):
    def make(self, **kw):
        self.sb = Sandbox(**kw)
        self.addCleanup(self.sb.close)
        return self.sb


class TenWorkers(ControllerCase):
    def test_ten_workers_twenty_tasks_each_stage_once(self):
        sb = self.make(n_tasks=20, workers=10, model_concurrency=2)
        sb.start()
        self.assertTrue(sb.wait_for(lambda: sum(1 for w in sb.workers() if w["pid"] and pid_alive(w["pid"])) == 10,
                                    timeout=30), "10 workers did not come up")
        self.assertTrue(sb.wait_for(lambda: all_settled(sb, 20), timeout=300), sb.tasks())
        status = sb.cli("status")
        self.assertEqual(len(re.findall(r"^  w\d\d ", status.stdout, re.M)), 10, status.stdout)
        sb.stop()

        for tid, t in sb.tasks().items():
            self.assertEqual((t["state"], t["ai_runs"]), ("held", 2), (tid, t))
            self.assertIn("never grants 'accepted'", t["reason"])
            contract = sb.remote_contract(tid)
            self.assertEqual(contract["status"], "passed", tid)
            self.assertEqual(completed_worker_runs(contract, sb), 1, tid)
        # one model call per task and role: nothing ran twice
        calls = calls_by_task(sb)
        self.assertEqual(len(calls), 40)
        self.assertTrue(all(len(v) == 1 for v in calls.values()), {k: len(v) for k, v in calls.items() if len(v) > 1})
        # each (task, stage) claimed exactly once
        claims = {}
        for e in sb.events("claimed"):
            claims[(e["task_id"], e["stage"])] = claims.get((e["task_id"], e["stage"]), 0) + 1
        self.assertEqual(set(claims.values()), {1})
        # model concurrency limit held, and was actually used
        self.assertEqual(max_overlap(sb.spy_calls()), 2)
        self.assertEqual(sb.events("reclaimed"), [])

    def test_model_concurrency_is_configurable(self):
        sb = self.make(n_tasks=6, workers=10, model_concurrency=1, stub={"worker_sleep": 0.5})
        sb.start()
        self.assertTrue(sb.wait_for(lambda: all_settled(sb, 6), timeout=200), sb.tasks())
        self.assertEqual(max_overlap(sb.spy_calls()), 1)


class Recovery(ControllerCase):
    def busy_worker(self, sb, stage="work"):
        found = {}

        def pick():
            for w in sb.workers():
                if w["state"] == "busy" and w["stage"] == stage and w["pid"]:
                    found.update(w)
                    return True
            return False
        self.assertTrue(sb.wait_for(pick, timeout=60))
        return found

    def test_killed_worker_is_replaced_and_its_task_finishes_once(self):
        sb = self.make(n_tasks=2, workers=3, model_concurrency=2, stub={"worker_sleep": 6})
        sb.start()
        victim = self.busy_worker(sb)
        time.sleep(1.0)  # let the stage start its agent
        os.kill(victim["pid"], signal.SIGKILL)
        self.assertTrue(sb.wait_for(lambda: all_settled(sb, 2), timeout=200), sb.tasks())
        sb.stop()
        self.assertTrue(sb.events("reclaimed"), "lease of the killed worker was not reclaimed")
        self.assertTrue(sb.events("orphan_killed"), "the dead worker's stage process was not stopped")
        # the orphaned agent was killed before it finished: one recorded worker run per task
        per_task = calls_by_task(sb)
        for tid in sb.task_ids:
            self.assertEqual(len(per_task[(tid, "worker")]), 1, tid)
        # the replacement took over at once, not after a worker error and its 60 s retry delay
        errors = [e for e in sb.events("released") if "worker error" in (e["detail"] or "")]
        self.assertEqual(errors, [])
        restarted = [w for w in sb.workers() if w["worker_id"] == victim["worker_id"]][0]
        self.assertGreaterEqual(restarted["restarts"], 1)
        for tid in sb.task_ids:
            contract = sb.remote_contract(tid)
            self.assertEqual(contract["status"], "passed")
            self.assertEqual(completed_worker_runs(contract, sb), 1, tid)
        # the killed run and its replacement never overlapped
        for (tid, role), calls in calls_by_task(sb).items():
            calls.sort(key=lambda c: c["start"])
            for a, b in zip(calls, calls[1:]):
                self.assertLessEqual(a["end"], b["start"] + 0.05, (tid, role))

    def test_controller_killed_then_restarted_resumes(self):
        sb = self.make(n_tasks=4, workers=4, model_concurrency=2, stub={"worker_sleep": 3})
        proc = sb.start()
        self.busy_worker(sb)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
        # orphaned workers notice and stop by themselves
        self.assertTrue(sb.wait_for(lambda: not any(w["pid"] and pid_alive(w["pid"]) for w in sb.workers()),
                                    timeout=60), sb.workers())
        sb.start()
        self.assertTrue(sb.wait_for(lambda: all_settled(sb, 4), timeout=240), sb.tasks())
        sb.stop()
        for tid in sb.task_ids:
            contract = sb.remote_contract(tid)
            self.assertEqual(contract["status"], "passed", tid)
            self.assertEqual(completed_worker_runs(contract, sb), 1, tid)

    def test_graceful_stop_leaves_no_lease(self):
        sb = self.make(n_tasks=3, workers=3, stub={"worker_sleep": 4})
        sb.start()
        self.busy_worker(sb)
        sb.stop(timeout=60)
        self.assertEqual(sb.proc.returncode, 0)
        self.assertFalse([t for t in sb.tasks().values() if t["state"] == "leased"])
        self.assertFalse([w for w in sb.workers() if w["pid"] and pid_alive(w["pid"])])
        sb.start()
        self.assertTrue(sb.wait_for(lambda: all_settled(sb, 3), timeout=200), sb.tasks())


class Limits(ControllerCase):
    def test_usage_limit_backs_off_and_resumes(self):
        sb = self.make(n_tasks=3, workers=3, model_concurrency=1, stub={"rate_limit_claude": 1})
        sb.start()
        self.assertTrue(sb.wait_for(lambda: all_settled(sb, 3), timeout=240), sb.tasks())
        sb.stop()
        backoff = sb.events("backoff")
        self.assertEqual(len(backoff), 1)
        detail = json.loads(backoff[0]["detail"])
        self.assertEqual(detail["agent"], "claude-code")
        limited = [c for c in sb.spy_calls() if c.get("rate_limited")]
        self.assertEqual(len(limited), 1)
        later = [c for c in sb.spy_calls() if c["cli"] == "claude" and c["start"] > limited[0]["end"]]
        self.assertTrue(later)
        self.assertGreaterEqual(min(c["start"] for c in later), backoff[0]["ts"] + detail["seconds"] - 0.5)
        states = sorted(t["state"] + ":" + (t["reason"] or "")[:9] for t in sb.tasks().values())
        self.assertEqual(sum(s.startswith("held:escalated") for s in states), 1, states)  # the limited run's task
        self.assertEqual(sum(s.startswith("held:waiting") for s in states), 2, states)

    def test_ai_run_budget_holds_the_task(self):
        sb = self.make(n_tasks=1, workers=2, config={"max_ai_runs_per_task": 1})
        sb.start()
        self.assertTrue(sb.wait_for(lambda: all_settled(sb, 1), timeout=120), sb.tasks())
        t = sb.tasks()["T-0001"]
        self.assertIn("AI run budget", t["reason"])
        self.assertEqual(t["stage"], "review")
        self.assertEqual([c["role"] for c in sb.spy_calls()], ["worker"])

    def test_unauthenticated_reviewer_is_parked_then_resumes(self):
        sb = self.make(n_tasks=2, workers=3, stub={"codex_logged_in": False})
        sb.start()
        self.assertTrue(sb.wait_for(lambda: len(sb.tasks()) == 2 and all(
            t["stage"] == "review" and t["state"] == "queued" for t in sb.tasks().values()), timeout=120), sb.tasks())
        time.sleep(3)
        self.assertEqual([c["cli"] for c in sb.spy_calls()].count("codex"), 0)
        self.assertEqual(sb.events("claimed")[-1]["stage"], "check")  # review never claimed
        sb.set_stub({"codex_logged_in": True})
        self.assertTrue(sb.wait_for(lambda: all_settled(sb, 2), timeout=120), sb.tasks())
        for tid in sb.task_ids:
            self.assertEqual(sb.remote_contract(tid)["status"], "passed")


class SourceAndTrust(ControllerCase):
    def test_only_contracts_on_main_are_taken(self):
        sb = self.make(n_tasks=1, workers=2)
        side = sb.seed
        subprocess.run(["git", "switch", "-q", "-c", "feature/x"], cwd=side, check=True)
        from controller_harness import contract
        (side / "tasks" / "T-0099").mkdir(parents=True)
        (side / "tasks" / "T-0099" / "task.json").write_text(json.dumps(contract("T-0099")))
        draft = contract("T-0098")
        draft["status"], draft["history"] = "draft", []
        subprocess.run(["git", "add", "-A"], cwd=side, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "side"],
                       cwd=side, check=True)
        subprocess.run(["git", "push", "-q", "origin", "feature/x"], cwd=side, check=True)
        r = sb.cli("sync-once")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(set(sb.tasks()), {"T-0001"})

    def test_task_branch_cannot_replace_the_runner(self):
        sb = self.make(n_tasks=1, workers=2)
        # a task branch on the remote whose scripts/aiwos.py has been replaced
        seed = sb.seed
        subprocess.run(["git", "switch", "-q", "-c", "aiwos/task/T-0001"], cwd=seed, check=True)
        (seed / "scripts" / "aiwos.py").write_text("import sys\nprint('replaced runner')\nsys.exit(0)\n")
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "evil"], cwd=seed,
                       check=True)
        subprocess.run(["git", "push", "-q", "origin", "aiwos/task/T-0001"], cwd=seed, check=True)
        sb.start()
        self.assertTrue(sb.wait_for(lambda: all_settled(sb, 1), timeout=120), sb.tasks())
        logs = "".join(p.read_text() for p in (sb.state / "logs" / "stages").rglob("*.log"))
        self.assertNotIn("replaced runner", logs)  # the trusted runner ran every stage
        # and the publish gate (Security Fix 002) keeps the planted change off the remote
        refused = [json.loads(e["detail"]) for e in sb.events("publish_gate_refused")]
        self.assertTrue(any("scripts/aiwos.py" in p for r in refused for p in r), refused)
        remote_head = git(sb.tmp, "--git-dir", str(sb.remote), "rev-parse", "aiwos/task/T-0001")
        self.assertEqual(remote_head, subprocess.run(["git", "rev-parse", "HEAD"], cwd=seed, capture_output=True,
                                                     text=True).stdout.strip())  # nothing new pushed

    def test_github_unreachable_at_start_then_recovers(self):
        sb = self.make(n_tasks=1, workers=2)
        hidden = sb.tmp / "remote-offline.git"
        sb.remote.rename(hidden)  # "GitHub is down": fetch fails
        proc = sb.start()
        self.assertTrue(sb.wait_for(lambda: sb.events("sync_failed"), timeout=30))
        self.assertIsNone(proc.poll())  # the controller keeps running
        hidden.rename(sb.remote)
        self.assertTrue(sb.wait_for(lambda: all_settled(sb, 1), timeout=120), sb.tasks())
        self.assertEqual(sb.remote_contract("T-0001")["status"], "passed")

    def test_push_lost_after_final_stage_is_republished(self):
        sb = self.make(n_tasks=1, workers=2, config={"publish": "none"})
        sb.start()
        self.assertTrue(sb.wait_for(lambda: all_settled(sb, 1), timeout=120), sb.tasks())
        sb.stop()
        self.assertIsNone(sb.remote_contract("T-0001"))  # nothing was pushed
        cfg = json.loads(sb.config.read_text())
        cfg["publish"] = "git"
        sb.config.write_text(json.dumps(cfg))
        r = sb.cli("sync-once")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sb.remote_contract("T-0001")["status"], "passed")
        self.assertTrue(sb.events("republished"))

    def test_stage_env_drops_tokens(self):
        cfg = {"state_dir": "/tmp/x", "workspaces_dir": "/tmp/x/ws", "extra_path": [], "agent_bins": {}}
        os.environ["GH_TOKEN"] = "sentinel"
        try:
            env = stage_env(cfg, "/tmp/wt", 1)
        finally:
            del os.environ["GH_TOKEN"]
        self.assertNotIn("GH_TOKEN", env)
        self.assertEqual(env["AIWOS_ROOT"], "/tmp/wt")

    def test_github_mode_without_isolation_fails_closed(self):
        """Security Fix 002: publish=github uses GH_TOKEN. Without a verified
        agent-user boundary nothing is pushed and gh is never called, while the
        tasks themselves still run and are recorded locally."""
        sb = self.make(n_tasks=2, workers=2, config={"publish": "github", "github_repo": "example/repo"})
        gh_log = sb.tmp / "gh.log"
        fake = sb.bin / "gh"
        fake.write_text(f"#!/bin/sh\necho \"$@\" >> {gh_log}\n")
        fake.chmod(0o755)
        os.environ["PATH"] = f"{sb.bin}:{os.environ['PATH']}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", os.environ["PATH"].split(":", 1)[1]))
        sb.start()
        self.assertTrue(sb.wait_for(lambda: all_settled(sb, 2), timeout=180), sb.tasks())
        sb.stop()
        self.assertFalse(gh_log.exists(), "gh was called although publishing must fail closed")
        for tid in sb.task_ids:
            self.assertIsNone(sb.remote_contract(tid))  # nothing pushed
        blocked = [e for e in sb.events("publish_blocked")]
        self.assertTrue(blocked)
        self.assertIn("fail closed", blocked[0]["detail"])
        self.assertTrue(all(t["gate_sha"] for t in sb.tasks().values()))  # verified, kept local


def _claim_all(db_path, worker_id, out):
    store = Store(db_path)
    got = []
    while True:
        task = store.claim(worker_id, os.getpid(), 60, set(), model_slots_needed=False)
        if task is None:
            break
        got.append(task["task_id"])
    out.put(got)


class QueueAtomicity(unittest.TestCase):
    def test_ten_processes_never_claim_the_same_task(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "q.db")
            store = Store(db)
            store.init(10, 2)
            for i in range(200):
                store.upsert_new(f"Q-{i:04d}", "x", "b", "check", None, "queued")
            out = multiprocessing.Queue()
            procs = [multiprocessing.Process(target=_claim_all, args=(db, w, out)) for w in range(1, 11)]
            for p in procs:
                p.start()
            claimed = [tid for _ in procs for tid in out.get(timeout=60)]
            for p in procs:
                p.join()
            self.assertEqual(len(claimed), 200)
            self.assertEqual(len(set(claimed)), 200)

    def test_model_slots_are_a_hard_limit(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(os.path.join(d, "q.db"))
            store.init(10, 2)
            for i in range(5):
                store.upsert_new(f"M-{i}", "x", "b", "work", "claude-code", "queued")
            got = [store.claim(w, os.getpid(), 60, {"claude-code"}) for w in range(1, 6)]
            self.assertEqual(sum(1 for g in got if g), 2)


class Config(unittest.TestCase):
    def write(self, data):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(data, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_more_than_ten_workers_rejected(self):
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.write({"repo_dir": "/tmp", "state_dir": "/tmp", "workers": 11}))

    def test_defaults(self):
        cfg = config_mod.load(self.write({"repo_dir": "/tmp", "state_dir": "/tmp"}))
        self.assertEqual((cfg["workers"], cfg["model_concurrency"]), (10, 2))

    def test_unknown_key_rejected(self):
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.write({"repo_dir": "/tmp", "state_dir": "/tmp", "auto_merge": True}))


class ActionsPolicy(unittest.TestCase):
    """GitHub Actions: manual (workflow_dispatch) only, never on a timer or a push."""

    def test_workflows_are_manual_only(self):
        wf = ROOT / ".github" / "workflows"
        files = list(wf.glob("*.y*ml")) if wf.is_dir() else []
        for f in files:
            text = f.read_text()
            self.assertIn("workflow_dispatch", text, f.name)
            for trigger in ("schedule:", "push:", "pull_request:", "pull_request_target:", "workflow_run:"):
                self.assertNotIn(trigger, text, f"{f.name} uses {trigger}")


if __name__ == "__main__":
    unittest.main()
