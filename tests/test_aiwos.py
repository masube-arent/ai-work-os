"""End-to-end tests for scripts/aiwos.py with stub CLIs.

Each test copies the working tree into a fresh git repository, so runs,
branches and contract updates never touch this checkout. The stub `claude` and
`codex` executables are written per test; they never call a model. What they
prove is the runner's behaviour: dry run, isolation, records, hashes,
independence, judging, and that every stop condition stops.

Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE_TOKEN = "ghp_" + "Z9y8" * 9  # assembled at runtime, never committed

STUB = r'''#!/usr/bin/env python3
import json, os, sys
CLI = {cli!r}
MODE = {mode!r}
SPY = {spy!r}
args = sys.argv[1:]
if args == ["--version"]:
    print("9.9.9 (stub " + CLI + ")"); sys.exit(0)
if CLI == "claude" and args[:2] == ["auth", "status"]:
    print(json.dumps({{"loggedIn": MODE != "logged_out", "authMethod": "api_key" if MODE == "api_key" else "oauth_token",
                      "apiProvider": "firstParty", "email": "someone@example.com"}})); sys.exit(0)
if CLI == "codex" and args[:2] == ["login", "status"]:
    if MODE == "logged_out":
        print("Not logged in"); sys.exit(1)
    print("Logged in using ChatGPT"); sys.exit(0)
prompt = sys.stdin.read()
with open(SPY, "a") as f:
    f.write(json.dumps({{"cli": CLI, "argv": args, "cwd": os.getcwd(), "env": sorted(os.environ)}}) + "\n")
reviewer = ("--json-schema" in args) or ("read-only" in args)
REVIEW = {{"criteria": [{{"criterion_id": "AC1", "verdict": "PASS", "reason": "ok"}},
                        {{"criterion_id": "AC2", "verdict": "PASS", "reason": "ok"}},
                        {{"criterion_id": "AC3", "verdict": "FAIL" if MODE == "review_fail" else "PASS", "reason": "r"}}],
          "defects": [{{"severity": "high", "dimension": "completeness", "file": "greet.py", "description": "d"}}]
                     if MODE == "review_fail" else [], "summary": "stub review"}}
if MODE == "sleep":
    import time; time.sleep(30)
if reviewer:
    if MODE == "reviewer_edits":
        open("examples/development/src/greet.py", "a").write("# reviewer was here\n")
    if CLI == "codex":
        out = args[args.index("-o") + 1]
        open(out, "w").write(json.dumps(REVIEW))
        print(json.dumps({{"type": "thread.started", "thread_id": "codex-thread-1"}}))
    else:
        print(json.dumps({{"type": "result", "is_error": False, "session_id": "claude-review-session",
                          "num_turns": 1, "total_cost_usd": 0.01, "result": "",
                          "modelUsage": {{"stub-model": {{"inputTokens": 10, "outputTokens": 5}}}},
                          "structured_output": REVIEW}}))
    sys.exit(0)
src = "examples/development/src/greet.py"
if MODE == "out_of_scope":
    open("README.md", "a").write("\nscope violation\n")
if MODE == "leak":
    print("here is a token " + {token!r})
if MODE == "noop":
    pass
elif MODE == "broken":
    open(src, "w").write("def greet(name, lang='en'):\n    return 'wrong'\n")
else:
    open(src, "w").write(
        'def greet(name, lang="en"):\n'
        '    """lang: "en" or "ja". Other values fall back to English."""\n'
        '    if lang == "ja":\n'
        '        return f"こんにちは、{{name}}さん"\n'
        '    return f"Hello, {{name}}!"\n')
if MODE == "touch_task":
    path = os.path.join({repo!r}, "examples/development/task.json")
    t = json.load(open(path)); t["constraints"].append("edited concurrently"); json.dump(t, open(path, "w"))
if CLI == "codex":
    print(json.dumps({{"type": "thread.started", "thread_id": "codex-worker-thread"}}))
else:
    print(json.dumps({{"type": "result", "is_error": False, "session_id": "claude-worker-session",
                      "num_turns": 3, "total_cost_usd": 0.02, "result": "implemented",
                      "modelUsage": {{"stub-model": {{"inputTokens": 100, "outputTokens": 50}}}}}}))
'''


class Sandbox:
    """A throwaway copy of the repository with stub agent CLIs."""

    def __init__(self, claude="ok", codex="ok"):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiwos-test-"))
        self.repo = self.tmp / "repo"
        shutil.copytree(ROOT, self.repo, ignore=shutil.ignore_patterns(".git", "runs", "__pycache__"))
        # start every sandbox from the frozen 'ready' contracts, whatever state
        # the live examples are in
        shutil.copy(ROOT / "tests/fixtures/dev-0001-ready.json", self.repo / "examples/development/task.json")
        shutil.copy(ROOT / "tests/fixtures/dev-0002-ready.json", self.repo / "examples/development/task-swap.json")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base"]):
            subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)
        self.spy = self.tmp / "spy.jsonl"
        self.bins = {}
        for cli, mode in (("claude", claude), ("codex", codex)):
            path = self.tmp / f"stub-{cli}"
            path.write_text(STUB.format(cli=cli, mode=mode, spy=str(self.spy), token=FAKE_TOKEN,
                                        repo=str(self.repo)))
            path.chmod(0o755)
            self.bins[cli] = str(path)

    def aiwos(self, *args, extra_env=None):
        env = dict(os.environ, AIWOS_CLAUDE_BIN=self.bins["claude"], AIWOS_CODEX_BIN=self.bins["codex"],
                   AIWOS_WORKSPACES=str(self.tmp / "ws"), GH_TOKEN="sentinel-gh-token-value",
                   ANTHROPIC_API_KEY="sentinel-anthropic-key-value")
        env.update(extra_env or {})
        return subprocess.run([sys.executable, str(self.repo / "scripts" / "aiwos.py"), *args], cwd=self.repo,
                              capture_output=True, text=True, env=env, timeout=120)

    def task(self, rel="examples/development/task.json"):
        return json.loads((self.repo / rel).read_text())

    def validate(self, rel="examples/development/task.json"):
        return subprocess.run([sys.executable, str(self.repo / "scripts" / "validate-task.py"), rel],
                              cwd=self.repo, capture_output=True, text=True)

    def spy_calls(self):
        if not self.spy.exists():
            return []
        return [json.loads(line) for line in self.spy.read_text().splitlines()]

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


TASK = "examples/development/task.json"
SWAP = "examples/development/task-swap.json"


class Base(unittest.TestCase):
    claude, codex = "ok", "ok"

    def setUp(self):
        self.sb = Sandbox(self.claude, self.codex)

    def tearDown(self):
        self.sb.close()

    def ok(self, result, code=0):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result


class FullCycle(Base):
    def test_worker_check_review_judge_passes_and_validates(self):
        sb = self.sb
        self.ok(sb.aiwos("run", TASK, "--role", "worker", "--execute"))
        t = sb.task()
        self.assertEqual(t["status"], "review")
        worker = [e for e in t["evidence"] if e["kind"] == "run_record"][0]
        run = json.loads((sb.repo / worker["path"]).read_text())
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["changed_files"], ["examples/development/src/greet.py"])
        self.assertEqual(run["session_id_reported"], "claude-worker-session")
        # the worker's change is its own branch, and main is untouched
        branch = subprocess.run(["git", "rev-parse", run["branch"]], cwd=sb.repo, capture_output=True,
                                text=True).stdout.strip()
        self.assertEqual(branch, run["result_commit"])
        self.assertNotIn("こんにちは", (sb.repo / "examples/development/src/greet.py").read_text())

        self.ok(sb.aiwos("check", TASK, "--execute"))
        self.ok(sb.aiwos("run", TASK, "--role", "reviewer", "--execute"))
        self.ok(sb.aiwos("judge", TASK, "--write"))
        t = sb.task()
        self.assertEqual(t["status"], "passed")
        self.assertEqual({q["verdict"] for q in t["quality"].values()}, {"PASS"})
        self.assertEqual(t["quality"]["correctness"]["judged_by"], "aiwos-runner")
        self.assertEqual(t["quality"]["completeness"]["judged_by"], "codex-cli")
        self.ok(sb.validate())

        # Final approval is never granted from the CLI (Security Fix 001); see HumanApprovalSpoofing.

        m = json.loads(self.ok(sb.aiwos("metrics", "--json")).stdout)
        row = m["worker_runs"][0]
        self.assertEqual((row["checks_passed"], row["checks_total"], row["defects"]), (2, 2, 0))
        self.assertEqual(row["models_reported"], ["stub-model"])
        self.assertEqual(row["task_status_now"], "passed")

    def test_tampered_evidence_is_detected(self):
        sb = self.sb
        self.ok(sb.aiwos("run", TASK, "--role", "worker", "--execute"))
        diff = [e for e in sb.task()["evidence"] if e["kind"] == "diff"][0]
        with open(sb.repo / diff["path"], "a") as f:
            f.write("tampered\n")
        r = sb.validate()
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not match the recorded hash", r.stdout)

    def test_child_env_has_no_tokens(self):
        sb = self.sb
        self.ok(sb.aiwos("run", TASK, "--role", "worker", "--execute"))
        env = sb.spy_calls()[0]["env"]
        for name in ("GH_TOKEN", "ANTHROPIC_API_KEY", "AIWOS_CLAUDE_BIN"):
            self.assertNotIn(name, env)

    def test_worker_runs_outside_the_repository_without_runs_dir(self):
        sb = self.sb
        (sb.repo / "runs" / "OTHER-0001").mkdir(parents=True)
        (sb.repo / "runs" / "OTHER-0001" / "secret-answer.txt").write_text("answer")
        subprocess.run(["git", "add", "-A"], cwd=sb.repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "r"],
                       cwd=sb.repo, check=True)
        self.ok(sb.aiwos("run", TASK, "--role", "worker", "--execute"))
        cwd = Path(sb.spy_calls()[0]["cwd"])
        self.assertFalse(str(cwd).startswith(str(sb.repo)))
        self.assertFalse((cwd / "runs").exists())


def drive_to_passed(test):
    sb = test.sb
    for args in (("run", TASK, "--role", "worker", "--execute"), ("check", TASK, "--execute"),
                 ("run", TASK, "--role", "reviewer", "--execute"), ("judge", TASK, "--write")):
        test.ok(sb.aiwos(*args))
    test.assertEqual(sb.task()["status"], "passed")


class HumanApprovalSpoofing(Base):
    """Security Fix 001: `--by human` is a claim, not an identity. Until an
    authenticated approval channel exists, nothing reaches 'accepted'."""

    def test_cli_claiming_human_cannot_accept(self):
        sb = self.sb
        drive_to_passed(self)
        before = (sb.repo / TASK).read_bytes()
        r = sb.aiwos("transition", TASK, "--to", "accepted", "--by", "human")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("cannot be verified", r.stderr)
        self.assertEqual((sb.repo / TASK).read_bytes(), before)  # contract untouched

    def test_cli_agent_cannot_accept(self):
        sb = self.sb
        drive_to_passed(self)
        for actor in ("codex-cli", "claude-code", "aiwos-runner", "chatgpt"):
            r = sb.aiwos("transition", TASK, "--to", "accepted", "--by", actor)
            self.assertNotEqual(r.returncode, 0, actor)
        self.assertEqual(sb.task()["status"], "passed")

    def test_hand_edited_acceptance_fails_validation(self):
        sb = self.sb
        drive_to_passed(self)
        t = sb.task()
        t["status"] = "accepted"
        t["history"].append({"from": "passed", "to": "accepted", "at": "2099-01-01T00:00:00Z", "by": "human",
                             "note": "approved"})
        (sb.repo / TASK).write_text(json.dumps(t, ensure_ascii=False))
        r = sb.validate()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("no verifiable human approval", r.stdout)

    def test_cli_human_claim_on_other_transitions_is_marked_unverified(self):
        sb = self.sb
        self.ok(sb.aiwos("transition", TASK, "--to", "suspended", "--by", "human", "--reason", "pause"))
        step = sb.task()["history"][-1]
        self.assertEqual((step["to"], step["by"]), ("suspended", "human"))
        self.assertIn("unverified", step["note"])


class Rework(Base):
    codex = "review_fail"

    def test_blocking_defect_sends_task_to_rework_and_counts_retry(self):
        sb = self.sb
        self.ok(sb.aiwos("run", TASK, "--role", "worker", "--execute"))
        self.ok(sb.aiwos("check", TASK, "--execute"))
        self.ok(sb.aiwos("run", TASK, "--role", "reviewer", "--execute"))
        self.ok(sb.aiwos("judge", TASK, "--write"))
        t = sb.task()
        self.assertEqual((t["status"], t["retry_count"]), ("rework", 1))
        self.assertEqual(t["quality"]["completeness"]["verdict"], "FAIL")
        self.ok(sb.validate())
        # the next worker prompt carries the review findings
        r = self.ok(sb.aiwos("run", TASK, "--role", "worker"))
        self.assertIn("前回レビューの指摘", r.stdout)


class MechanicalFailure(Base):
    claude = "broken"

    def test_failing_command_is_fail_even_if_reviewer_passes(self):
        sb = self.sb
        self.ok(sb.aiwos("run", TASK, "--role", "worker", "--execute"))
        self.ok(sb.aiwos("check", TASK, "--execute"))
        self.ok(sb.aiwos("run", TASK, "--role", "reviewer", "--execute"))
        r = self.ok(sb.aiwos("judge", TASK))
        self.assertIn("decision: review -> rework", r.stdout)
        self.assertEqual(sb.task()["status"], "review")  # dry run wrote nothing


class DryRun(Base):
    def test_dry_run_executes_nothing(self):
        sb = self.sb
        before = (sb.repo / TASK).read_bytes()
        r = self.ok(sb.aiwos("run", TASK, "--role", "worker"))
        self.assertIn("dry run: nothing was executed", r.stdout)
        self.assertEqual(sb.spy_calls(), [])
        self.assertEqual((sb.repo / TASK).read_bytes(), before)
        self.assertFalse((sb.repo / "runs").exists())


class Stops(Base):
    def assertEscalated(self, result, word):
        self.assertEqual(result.returncode, 5, result.stdout + result.stderr)
        t = self.sb.task()
        self.assertEqual(t["status"], "escalated")
        self.assertIn(word, t["escalation_reason"])
        self.ok(self.sb.validate())


class SecretLeak(Stops):
    claude = "leak"

    def test_secret_in_output_stops_and_is_not_stored(self):
        r = self.sb.aiwos("run", TASK, "--role", "worker", "--execute")
        self.assertEscalated(r, "security_violation")
        for f in (self.sb.repo / "runs").rglob("*"):
            if f.is_file():
                self.assertNotIn(FAKE_TOKEN, f.read_text(errors="replace"))
        branches = subprocess.run(["git", "branch", "--list", "run/*"], cwd=self.sb.repo, capture_output=True,
                                  text=True).stdout
        self.assertEqual(branches.strip(), "")


class OutOfScope(Stops):
    claude = "out_of_scope"

    def test_change_outside_allowed_paths_stops(self):
        r = self.sb.aiwos("run", TASK, "--role", "worker", "--execute")
        self.assertEscalated(r, "scope_violation")


class ReviewerEdits(Stops):
    codex = "reviewer_edits"

    def test_reviewer_that_modifies_the_tree_stops(self):
        sb = self.sb
        self.ok(sb.aiwos("run", TASK, "--role", "worker", "--execute"))
        self.ok(sb.aiwos("check", TASK, "--execute"))
        r = sb.aiwos("run", TASK, "--role", "reviewer", "--execute")
        self.assertEscalated(r, "modified the tree it reviewed")


class Timeout(Base):
    claude = "sleep"

    def test_timeout_kills_and_escalates(self):
        r = self.sb.aiwos("run", TASK, "--role", "worker", "--execute", "--timeout", "2")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        t = self.sb.task()
        self.assertEqual(t["status"], "escalated")
        self.assertIn("timeout", t["escalation_reason"])


class NoChanges(Base):
    claude = "noop"

    def test_worker_without_changes_is_not_completed(self):
        r = self.sb.aiwos("run", TASK, "--role", "worker", "--execute")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no changes", self.sb.task()["escalation_reason"])


class Concurrency(Base):
    claude = "touch_task"

    def test_concurrent_edit_is_a_conflict_not_an_overwrite(self):
        r = self.sb.aiwos("run", TASK, "--role", "worker", "--execute")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("edited concurrently", self.sb.task()["constraints"])

    def test_second_run_while_one_is_active_is_refused(self):
        sb = self.sb
        (sb.repo / "runs" / "DEV-0001").mkdir(parents=True)
        (sb.repo / "runs" / "DEV-0001" / ".active").write_text("DEV-0001-worker-x pid=1\n")
        r = sb.aiwos("run", TASK, "--role", "worker", "--execute")
        self.assertEqual(r.returncode, 4)
        self.assertEqual(sb.task()["status"], "ready")


class NotLoggedIn(Base):
    codex = "logged_out"

    def test_unauthenticated_cli_is_blocked_and_recorded(self):
        sb = self.sb
        r = sb.aiwos("run", SWAP, "--role", "worker", "--execute")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(sb.task(SWAP)["status"], "ready")
        records = list((sb.repo / "runs" / "DEV-0002").glob("*/run.json"))
        self.assertEqual(len(records), 1)
        rec = json.loads(records[0].read_text())
        self.assertEqual((rec["status"], rec["auth"]["logged_in"]), ("blocked", False))
        self.assertEqual(sb.spy_calls(), [])


class ApiKeyAuth(Base):
    claude = "api_key"

    def test_api_key_login_is_refused(self):
        r = self.sb.aiwos("run", TASK, "--role", "worker", "--execute")
        self.assertEqual(r.returncode, 3)
        rec = json.loads(next((self.sb.repo / "runs" / "DEV-0001").glob("*/run.json")).read_text())
        self.assertIn("allowlist", rec["stop_reason"])
        self.assertNotIn("someone@example.com", json.dumps(rec))


class SameExecutable(Base):
    def test_reviewer_on_the_workers_executable_is_refused(self):
        sb = self.sb
        self.ok(sb.aiwos("run", TASK, "--role", "worker", "--execute"))
        self.ok(sb.aiwos("check", TASK, "--execute"))
        # The reviewer label is codex-cli, but the binary is the one the worker ran.
        r = sb.aiwos("run", TASK, "--role", "reviewer", "--execute",
                     extra_env={"AIWOS_CODEX_BIN": sb.bins["claude"]})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("same executable", r.stderr)
        self.assertEqual([c["cli"] for c in sb.spy_calls()], ["claude"])


class SwapStructure(Base):
    def test_swapped_roles_run_from_the_same_base_on_separate_branches(self):
        sb = self.sb
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=sb.repo, capture_output=True,
                              text=True).stdout.strip()
        self.ok(sb.aiwos("run", TASK, "--role", "worker", "--execute", "--base", base))
        self.ok(sb.aiwos("run", SWAP, "--role", "worker", "--execute", "--base", base))
        calls = sb.spy_calls()
        self.assertEqual([c["cli"] for c in calls], ["claude", "codex"])
        self.assertNotEqual(calls[0]["cwd"], calls[1]["cwd"])
        runs = {p.parent.parent.name: json.loads(p.read_text()) for p in (sb.repo / "runs").glob("*/*/run.json")}
        self.assertEqual(runs["DEV-0001"]["base_commit"], runs["DEV-0002"]["base_commit"])
        self.assertNotEqual(runs["DEV-0001"]["branch"], runs["DEV-0002"]["branch"])


if __name__ == "__main__":
    unittest.main()
