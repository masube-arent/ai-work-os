"""Regression tests for the four long-running-operation risks found by the
independent review of PR #2 (v0.3 controller). Each was written to fail on
commit 6cba8ed and pass after the fix; see
tests/results/2026-10-11-v0.3-audit-fixes.md.

  1. errors retried forever (max_stage_attempts not applied to worker errors)
  2. a PR whose creation failed after a successful push is never retried
  3. an expired lease whose owner is still alive is handed to another worker
     while the owner's stage process keeps running
  4. GitHub-side contract changes are ignored; checkout prefers a stale local branch

All with stub agents; no model is called.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from controller_harness import ROOT, Sandbox, contract, git  # noqa: E402
from test_controller import all_settled, calls_by_task  # noqa: E402

sys.path.insert(0, str(ROOT))
from controller.store import Store  # noqa: E402

STAGES = ("work", "check", "review", "judge")


class Case(unittest.TestCase):
    def make(self, **kw):
        self.sb = Sandbox(**kw)
        self.addCleanup(self.sb.close)
        return self.sb

    def human_push(self, sb, branch, edit, message):
        """A person (not the controller) commits to `branch` on the remote."""
        clone = sb.tmp / f"human-{time.time_ns()}"
        git(sb.tmp, "clone", "-q", "-b", branch, str(sb.remote), str(clone))
        edit(clone)
        git(clone, "add", "-A")
        git(clone, "-c", "user.name=person", "-c", "user.email=p@example.invalid", "commit", "-q", "-m", message)
        git(clone, "push", "-q", "origin", branch)
        return git(clone, "rev-parse", "HEAD")


# ---------------------------------------------------------------- 1. retries

class RetryLimitStore(unittest.TestCase):
    """The worker-error path: claim, fail, requeue without resetting attempts."""

    def test_error_retries_stop_at_max_stage_attempts_for_every_stage(self):
        for stage in STAGES:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as d:
                store = Store(os.path.join(d, "q.db"))
                store.init(2, 2)
                agent = "claude-code" if stage in ("work", "review") else None
                store.upsert_new("T-1", "tasks/T-1/task.json", "aiwos/task/T-1", stage, agent, "queued")
                claims = 0
                for _ in range(10):
                    task = store.claim(1, os.getpid(), 60, {"claude-code"}, max_attempts=3)
                    if task is None:
                        break
                    claims += 1
                    store.finish(1, os.getpid(), "T-1", stage=stage, agent=agent, state="queued",
                                 reason="worker error: boom", reset_attempts=False, max_attempts=3)
                row = store.db.execute("SELECT * FROM tasks WHERE task_id='T-1'").fetchone()
                self.assertEqual(claims, 3)
                self.assertEqual((row["state"], row["stage"]), ("held", stage))
                self.assertIn("3 attempts", row["reason"])


class RetryLimitIntegration(Case):
    """A real exception inside the worker (a commit rejected by a git hook)
    at each stage, with max_stage_attempts=1: the task must be held, not
    retried every 60 s forever."""

    def fail_commits_of(self, sb, stage):
        hooks = sb.tmp / "hooks"
        hooks.mkdir()
        hook = hooks / "commit-msg"
        hook.write_text(f"#!/bin/sh\ngrep -q ': {stage} exit' \"$1\" && {{ echo 'rejected by test hook' >&2; exit 1; }}\n"
                        "exit 0\n")
        hook.chmod(0o755)
        git(sb.repo, "config", "core.hooksPath", str(hooks))

    def check_stage(self, stage, ai_runs):
        sb = self.make(n_tasks=1, workers=2, config={"max_stage_attempts": 1})
        self.fail_commits_of(sb, stage)
        sb.start()
        held = sb.wait_for(lambda: (sb.tasks().get("T-0001") or {}).get("state") == "held", timeout=40)
        t = sb.tasks().get("T-0001")
        self.assertTrue(held, t)
        self.assertEqual(t["stage"], stage)
        self.assertIn("attempts", t["reason"])
        self.assertEqual(t["ai_runs"], ai_runs)  # a model run inside a failed stage still counts
        claims = [e for e in sb.events("claimed") if e["stage"] == stage]
        self.assertEqual(len(claims), 1)

    def test_work(self):
        self.check_stage("work", 1)

    def test_check(self):
        self.check_stage("check", 1)

    def test_review(self):
        self.check_stage("review", 2)

    def test_judge(self):
        self.check_stage("judge", 2)


# ---------------------------------------------------------------- 2. PR retry

@unittest.skip("moved to tests/test_isolation.py: publish=github now requires verified isolation (Security Fix 002)")
class PullRequestRetry(Case):
    def test_pr_creation_failed_after_push_is_retried_once_it_can_succeed(self):
        sb = self.make(n_tasks=1, workers=2, config={"publish": "github"})
        log, broken = sb.tmp / "gh.log", sb.tmp / "gh-broken"
        broken.write_text("")
        fake = sb.bin / "gh"
        fake.write_text(f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
head = args[args.index("--head") + 1]
open({str(log)!r}, "a").write(json.dumps(args) + "\\n")
made = [json.loads(l) for l in open({str(log)!r})][:-1]
made = [a for a in made if a[:2] == ["pr", "create"] and a[a.index("--head") + 1] == head and "ok" in a[-1:]]
if args[:2] == ["pr", "list"]:
    print("https://example.invalid/pr/1" if made else "")
    sys.exit(0)
if os.path.exists({str(broken)!r}):
    print("HTTP 502: GitHub is having a bad day", file=sys.stderr)
    sys.exit(1)
open({str(log)!r}, "a").write(json.dumps(args + ["ok"]) + "\\n")
print("https://example.invalid/pr/1")
""")
        fake.chmod(0o755)
        os.environ["PATH"] = f"{sb.bin}:{os.environ['PATH']}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", os.environ["PATH"].split(":", 1)[1]))
        sb.start()
        self.assertTrue(sb.wait_for(lambda: all_settled(sb, 1), timeout=120), sb.tasks())
        self.assertEqual(sb.remote_contract("T-0001")["status"], "passed")  # every push succeeded
        broken.unlink()  # GitHub recovers; nothing new is pushed from here on

        def created():
            return [json.loads(l) for l in log.read_text().splitlines() if l.endswith('"ok"]')]
        self.assertTrue(sb.wait_for(lambda: len(created()) == 1, timeout=30), "PR was never retried")
        time.sleep(6)  # several more sync rounds
        self.assertEqual(len(created()), 1)  # and never duplicated


# ---------------------------------------------------------------- 3. live owner

class LiveOwnerExpiredLease(Case):
    def test_frozen_worker_is_stopped_before_its_task_is_handed_on(self):
        sb = self.make(n_tasks=1, workers=3, stub={"worker_sleep": 15},
                       config={"lease_ttl_s": 6, "heartbeat_s": 1})
        sb.start()
        owner = {}

        def busy():
            for w in sb.workers():
                if w["state"] == "busy" and w["stage"] == "work" and w["pid"]:
                    owner.update(w)
                    return True
        self.assertTrue(sb.wait_for(busy, timeout=60, interval=0.2))
        time.sleep(1.5)  # the stage process and its agent are running
        os.kill(owner["pid"], signal.SIGSTOP)  # alive, but no heartbeat any more
        self.addCleanup(lambda: _cont(owner["pid"]))
        self.assertTrue(sb.wait_for(lambda: all_settled(sb, 1), timeout=150), sb.tasks())
        sb.stop()
        calls = calls_by_task(sb)[("T-0001", "worker")]
        self.assertEqual(len(calls), 1, "the frozen worker's agent ran on beside its replacement")
        self.assertTrue(sb.events("stale_owner_stopped"))


class ModelRunsAreChargedBeforeTheyStart(unittest.TestCase):
    """Security Fix 002, item 8 (supersedes the reclaim-time charge of 427e222,
    which missed a worker killed between starting the stage and recording its
    pid). A model run is charged before its process exists."""

    def store(self, d):
        s = Store(os.path.join(d, "q.db"))
        s.init(2, 2)
        s.upsert_new("T-1", "x", "b", "work", "claude-code", "queued")
        return s

    def dead_pid(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    def test_killed_before_its_pid_was_recorded_still_counts(self):
        with tempfile.TemporaryDirectory() as d:
            store, dead = self.store(d), self.dead_pid()
            store.claim(1, dead, 60, {"claude-code"})
            self.assertTrue(store.charge_ai_run(1, dead, "T-1"))
            # the worker dies here: no set_child, no finish
            store.reclaim(3, [("T-1", dead)])
            self.assertEqual(store.get("T-1")["ai_runs"], 1)

    def test_a_completed_run_counts_once(self):
        with tempfile.TemporaryDirectory() as d:
            store = self.store(d)
            store.claim(1, os.getpid(), 60, {"claude-code"})
            store.charge_ai_run(1, os.getpid(), "T-1")
            store.finish(1, os.getpid(), "T-1", stage="review", agent="codex-cli", state="queued")
            self.assertEqual(store.get("T-1")["ai_runs"], 1)

    def test_runner_refusal_before_the_agent_started_is_refunded(self):
        with tempfile.TemporaryDirectory() as d:
            store = self.store(d)
            store.claim(1, os.getpid(), 60, {"claude-code"})
            store.charge_ai_run(1, os.getpid(), "T-1")
            store.refund_ai_run(1, os.getpid(), "T-1", "not logged in")
            self.assertEqual(store.get("T-1")["ai_runs"], 0)

    def test_only_the_lease_holder_can_charge(self):
        with tempfile.TemporaryDirectory() as d:
            store = self.store(d)
            store.claim(1, os.getpid(), 60, {"claude-code"})
            self.assertFalse(store.charge_ai_run(2, os.getpid(), "T-1"))
            self.assertEqual(store.get("T-1")["ai_runs"], 0)


class StageProcessFoundByLeaseTag(unittest.TestCase):
    def test_tagged_process_is_found_without_a_recorded_pid(self):
        from controller.procs import find_tagged
        tag = f"T-9:{os.getpid()}"
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             env={**os.environ, "AIWOS_LEASE_TAG": tag})
        try:
            self.assertEqual(find_tagged(tag), [p.pid])
            self.assertEqual(find_tagged("T-9:1"), [])
        finally:
            p.kill()
            p.wait()


class LeaseKeeperLimit(unittest.TestCase):
    def test_keeper_stops_renewing_after_the_longest_legitimate_stage(self):
        from controller.worker import LeaseKeeper
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "q.db")
            store = Store(db)
            store.init(1, 1)
            store.upsert_new("T-1", "x", "b", "check", None, "queued")
            store.claim(1, os.getpid(), 2, set())
            keeper = LeaseKeeper(db, 1, os.getpid(), "T-1", ttl=2, interval=0.2, max_duration=1.0)
            keeper.start()
            time.sleep(0.6)
            renewed = store.get("T-1")["lease_expires"]
            self.assertGreater(renewed, time.time() + 1.0)  # still renewing
            time.sleep(3.5)  # past max_duration and the ttl
            self.assertLess(store.get("T-1")["lease_expires"], time.time())  # left to expire
            self.assertIn(("T-1", os.getpid()), [(s["task_id"], s["lease_pid"]) for s in store.stale_leases()])
            keeper.done.set()
            keeper.join()


def _cont(pid):
    try:
        os.kill(pid, signal.SIGCONT)
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------- 4. GitHub state

class GitHubStateSync(Case):
    def test_human_fix_on_the_task_branch_is_taken_in(self):
        """escalated -> (person resets it to ready on GitHub) -> runs again."""
        sb = self.make(n_tasks=1, workers=2, model_concurrency=1, stub={"rate_limit_claude": 1})
        sb.start()
        self.assertTrue(sb.wait_for(lambda: (sb.tasks().get("T-0001") or {}).get("reason", "") and
                                    sb.tasks()["T-0001"]["reason"].startswith("escalated"), timeout=90), sb.tasks())
        self.assertTrue(sb.wait_for(lambda: (sb.remote_contract("T-0001") or {}).get("status") == "escalated",
                                    timeout=30))

        def to_ready(clone):
            r = subprocess.run([sys.executable, "scripts/aiwos.py", "transition", "tasks/T-0001/task.json", "--to",
                                "ready", "--by", "human", "--reason", "usage limit cleared"],
                               cwd=clone, capture_output=True, text=True, env={**os.environ, "AIWOS_ROOT": str(clone)})
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        human = self.human_push(sb, "aiwos/task/T-0001", to_ready, "person: escalated -> ready")
        self.assertTrue(sb.wait_for(lambda: (sb.remote_contract("T-0001") or {}).get("status") == "passed",
                                    timeout=120), sb.tasks())
        log = git(sb.tmp, "--git-dir", str(sb.remote), "log", "--format=%H", "aiwos/task/T-0001").split()
        self.assertIn(human, log)  # built on the person's commit, not over it

    def test_contract_change_on_main_before_work_is_taken_in(self):
        sb = self.make(n_tasks=1, workers=2, stub={"codex_logged_in": False},
                       task_kwargs={"worker": "codex-cli", "reviewer": "claude-code"})
        sb.start()
        self.assertTrue(sb.wait_for(lambda: (sb.tasks().get("T-0001") or {}).get("agent") == "codex-cli", timeout=30))
        time.sleep(2)

        def reassign(clone):
            p = clone / "tasks/T-0001/task.json"
            c = json.loads(p.read_text())
            c["assigned_agent"], c["reviewer"] = "claude-code", "codex-cli"
            p.write_text(json.dumps(c, ensure_ascii=False, indent=2))
        self.human_push(sb, "main", reassign, "person: give T-0001 to claude-code")
        self.assertTrue(sb.wait_for(lambda: ("T-0001", "worker") in calls_by_task(sb), timeout=60),
                        f"the change on main was never taken in: {sb.tasks()}")
        self.assertEqual(calls_by_task(sb)[("T-0001", "worker")][0]["cli"], "claude")

    def test_contract_change_on_main_after_work_started_is_held(self):
        sb = self.make(n_tasks=1, workers=2, stub={"codex_logged_in": False})
        sb.start()
        self.assertTrue(sb.wait_for(lambda: (sb.tasks().get("T-0001") or {}).get("stage") == "review", timeout=60))

        def change_criteria(clone):
            p = clone / "tasks/T-0001/task.json"
            c = json.loads(p.read_text())
            c["constraints"].append("added after work started")
            p.write_text(json.dumps(c, ensure_ascii=False, indent=2))
        self.human_push(sb, "main", change_criteria, "person: change T-0001 on main")
        self.assertTrue(sb.wait_for(lambda: sb.tasks()["T-0001"]["state"] == "held", timeout=30), sb.tasks())
        self.assertIn("changed on main", sb.tasks()["T-0001"]["reason"])

    def test_diverged_branch_is_held_and_nothing_is_overwritten(self):
        sb = self.make(n_tasks=1, workers=2)
        sb.start()
        self.assertTrue(sb.wait_for(lambda: all_settled(sb, 1), timeout=120), sb.tasks())
        # the controller holds a commit GitHub has not seen ...
        wt = sb.tmp / "local-wt"
        git(sb.repo, "worktree", "add", "-q", str(wt), "aiwos/task/T-0001")
        (wt / "tasks/T-0001/local.txt").write_text("controller-side change\n")
        git(wt, "add", "-A")
        git(wt, "-c", "user.name=c", "-c", "user.email=c@c", "commit", "-q", "-m", "local, unpublished")
        local = git(wt, "rev-parse", "HEAD")
        git(sb.repo, "worktree", "remove", "--force", str(wt))
        # ... while a person pushed a different commit to the same branch
        human = self.human_push(sb, "aiwos/task/T-0001",
                                lambda c: (c / "tasks/T-0001/human.txt").write_text("person's change\n"),
                                "person: note")
        self.assertTrue(sb.wait_for(lambda: "diverged" in (sb.tasks()["T-0001"]["reason"] or ""), timeout=30),
                        sb.tasks())
        self.assertEqual(sb.tasks()["T-0001"]["state"], "held")
        remote_head = git(sb.tmp, "--git-dir", str(sb.remote), "rev-parse", "aiwos/task/T-0001")
        self.assertEqual(remote_head, human)  # the person's commit was not overwritten
        self.assertEqual(git(sb.repo, "rev-parse", "refs/heads/aiwos/task/T-0001"), local)  # nor ours discarded


if __name__ == "__main__":
    unittest.main()
