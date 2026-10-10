"""Tests for scripts/validate-task.py.

Valid cases: both sample tasks, the template, and a fully evidenced passed task
built here. Invalid cases: one mutation per rule, so each rule is shown to fire
on its own and not only in combination.

Run: python3 -m unittest discover -s tests -v
"""
import copy
import importlib.util
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "validate-task.py"

spec = importlib.util.spec_from_file_location("validate_task", SCRIPT)
vt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vt)
VALIDATOR = vt.load_schema_validator()


def load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


# Rule tests start from a frozen copy of DEV-0001 in its 'ready' state; the
# live examples/development/task.json moves on as experiments run.
DEV_READY = "tests/fixtures/dev-0001-ready.json"


EVIDENCE_DIR = ROOT / ".test-evidence"  # gitignored; created and removed by this module
WORKER_COMMIT = "a" * 40


def tearDownModule():
    import shutil
    shutil.rmtree(EVIDENCE_DIR, ignore_errors=True)


def run_record(role, agent, cli, path, session, target=None, result=None):
    return {
        "run_schema_version": "0.2", "run_id": f"DEV-0001-{role}-20261010T010000Z", "task_id": "DEV-0001",
        "task_path": "examples/development/task.json", "task_type": "development", "role": role, "agent": agent,
        "status": "completed", "stop_reason": None, "cli": {"name": cli, "path": path, "version": "1.0"},
        "auth": {"checked": True, "logged_in": True, "method": "oauth_token", "provider": "firstParty"},
        "argv": [], "model_requested": None, "models_reported": None, "session_id_reported": session,
        "cost_usd_reported": None, "num_turns_reported": None, "base_commit": "b" * 40,
        "target_commit": target, "result_commit": result, "branch": None, "task_sha256_at_start": "c" * 64,
        "retry_count_at_start": 0, "started_at": "2026-10-10T01:00:00Z", "ended_at": "2026-10-10T01:01:00Z",
        "duration_s": 60.0, "exit_code": 0, "timeout_s": 900, "changed_files": [], "files": {},
        "security": {"secret_hits": [], "out_of_scope_paths": [],
                     "workspace_modified_by_reviewer": False if role == "reviewer" else None},
    }


def passed_task(mutate=None):
    """DEV-0001 carried through to 'passed', with real files whose hashes the
    validator checks: worker, check and reviewer run records, a check log and a
    review. `mutate(records)` may alter the run records before they are written.

    Test data only; the files live under .test-evidence/ and are deleted.
    """
    import tempfile
    EVIDENCE_DIR.mkdir(exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=EVIDENCE_DIR))
    records = {
        "worker": run_record("worker", "claude-code", "claude", "/usr/bin/claude", "s-worker", result=WORKER_COMMIT),
        "check": run_record("check", "aiwos-runner", "bash", "/bin/bash", None, target=WORKER_COMMIT),
        "reviewer": run_record("reviewer", "codex-cli", "codex", "/usr/bin/codex", "s-review", target=WORKER_COMMIT),
    }
    if mutate:
        mutate(records)
    files = {f"{k}.json": json.dumps(v).encode() for k, v in records.items()}
    files["ac.log"] = b"Ran 2 tests\nOK\n"
    files["review.json"] = b'{"summary": "ok"}'
    for name, data in files.items():
        (d / name).write_bytes(data)

    def ev(eid, kind, name, by, cids, **extra):
        e = {"id": eid, "kind": kind, "path": (d / name).relative_to(ROOT).as_posix(),
             "sha256": vt.file_sha256(d / name), "produced_by": by, "created_at": "2026-10-10T01:00:00Z",
             "criterion_ids": cids}
        e.update(extra)
        return e

    t = load(DEV_READY)
    t["status"] = "passed"
    t["evidence"] = [
        ev("W1", "run_record", "worker.json", "aiwos-runner", []),
        ev("C1", "run_record", "check.json", "aiwos-runner", []),
        ev("E1", "command_output", "ac.log", "aiwos-runner", ["AC1", "AC2"], command="python3 -m unittest -v",
           exit_code=0),
        ev("R1", "run_record", "reviewer.json", "aiwos-runner", []),
        ev("E2", "review_note", "review.json", "codex-cli", ["AC3"]),
    ]
    t["quality"] = {
        "spec_conformance": {"verdict": "PASS", "evidence_ids": ["E1"], "judged_by": "aiwos-runner"},
        "correctness": {"verdict": "PASS", "evidence_ids": ["E1"], "judged_by": "aiwos-runner"},
        "completeness": {"verdict": "PASS", "evidence_ids": ["E2", "R1"], "judged_by": "codex-cli"},
    }
    t["history"] += [
        {"from": "ready", "to": "in_progress", "at": "2026-10-10T00:10:00Z", "by": "aiwos-runner"},
        {"from": "in_progress", "to": "review", "at": "2026-10-10T00:50:00Z", "by": "aiwos-runner"},
        {"from": "review", "to": "passed", "at": "2026-10-10T01:10:00Z", "by": "aiwos-runner"},
    ]
    return t


class ValidTasks(unittest.TestCase):
    def assertValid(self, task):
        self.assertEqual(vt.validate_task(task, VALIDATOR), [])

    def test_development_sample(self):
        self.assertValid(load("examples/development/task.json"))

    def test_development_fixture(self):
        self.assertValid(load(DEV_READY))

    def test_document_compare_sample(self):
        self.assertValid(load("examples/document-compare/task.json"))

    def test_template(self):
        self.assertValid(load("contracts/task.example.json"))

    def test_both_samples_share_one_contract(self):
        dev = load("examples/development/task.json")
        doc = load("examples/document-compare/task.json")
        required = set(load("contracts/task.schema.json")["required"])
        self.assertLessEqual(required, set(dev))
        self.assertLessEqual(required, set(doc))
        self.assertNotEqual(dev["task_type"], doc["task_type"])

    def test_swap_sample_differs_only_in_roles(self):
        dev = load("examples/development/task.json")
        swap = load("examples/development/task-swap.json")
        self.assertEqual((swap["assigned_agent"], swap["reviewer"]), (dev["reviewer"], dev["assigned_agent"]))
        for key in ("acceptance_criteria", "failure_conditions", "constraints", "inputs", "allowed_paths",
                    "max_retries"):
            self.assertEqual(dev[key], swap[key], key)

    def test_v01_contract_still_valid(self):
        t = load(DEV_READY)
        t["schema_version"] = "0.1"
        del t["allowed_paths"]
        self.assertValid(t)

    def test_fully_evidenced_pass(self):
        self.assertValid(passed_task())

    def test_rework_cycle_counts_retry(self):
        t = load(DEV_READY)
        t["status"] = "in_progress"
        t["retry_count"] = 1
        t["history"] += [
            {"from": "ready", "to": "in_progress", "at": "2026-10-10T00:10:00Z", "by": "claude-code"},
            {"from": "in_progress", "to": "review", "at": "2026-10-10T00:20:00Z", "by": "claude-code"},
            {"from": "review", "to": "rework", "at": "2026-10-10T00:30:00Z", "by": "codex-cli"},
            {"from": "rework", "to": "in_progress", "at": "2026-10-10T00:40:00Z", "by": "claude-code"},
        ]
        self.assertValid(t)

    def test_suspend_and_resume(self):
        t = load(DEV_READY)
        t["status"] = "in_progress"
        t["history"] += [
            {"from": "ready", "to": "in_progress", "at": "2026-10-10T00:10:00Z", "by": "claude-code"},
            {"from": "in_progress", "to": "suspended", "at": "2026-10-10T00:20:00Z", "by": "human"},
            {"from": "suspended", "to": "in_progress", "at": "2026-10-11T00:00:00Z", "by": "claude-code"},
        ]
        self.assertValid(t)

    def test_human_acceptance(self):
        t = passed_task()
        t["status"] = "accepted"
        t["history"].append({"from": "passed", "to": "accepted", "at": "2026-10-10T02:00:00Z", "by": "human"})
        self.assertValid(t)


class InvalidTasks(unittest.TestCase):
    def assertRejected(self, task, pattern):
        errors = vt.validate_task(task, VALIDATOR)
        self.assertTrue(any(re.search(pattern, e) for e in errors),
                        f"expected an error matching {pattern!r}, got {errors}")

    def test_fixture_file_is_invalid(self):
        self.assertTrue(vt.validate_task(load("tests/fixtures/invalid-task.json"), VALIDATOR))

    # --- structure (schema) ---
    def test_missing_required_field(self):
        t = load(DEV_READY)
        del t["objective"]
        self.assertRejected(t, r"'objective' is a required property")

    def test_unknown_field(self):
        t = load(DEV_READY)
        t["priority"] = "high"
        self.assertRejected(t, r"Additional properties")

    def test_unknown_status(self):
        t = load(DEV_READY)
        t["status"] = "done"
        self.assertRejected(t, r"schema: status")

    def test_unknown_agent(self):
        t = load(DEV_READY)
        t["assigned_agent"] = "some-new-model"
        self.assertRejected(t, r"schema: assigned_agent")

    def test_no_acceptance_criteria(self):
        t = load(DEV_READY)
        t["acceptance_criteria"] = []
        self.assertRejected(t, r"schema: acceptance_criteria")

    def test_command_criterion_without_command(self):
        t = load(DEV_READY)
        del t["acceptance_criteria"][0]["verification"]["command"]
        self.assertRejected(t, r"'command' is a required property")

    def test_pass_without_evidence(self):
        t = load(DEV_READY)
        t["quality"]["correctness"] = {"verdict": "PASS", "evidence_ids": [], "judged_by": "codex-cli"}
        self.assertRejected(t, r"quality/correctness/evidence_ids")

    def test_escalated_without_reason(self):
        t = load(DEV_READY)
        t["status"] = "escalated"
        self.assertRejected(t, r"schema: escalation_reason")

    def test_retry_cap_above_policy(self):
        t = load(DEV_READY)
        t["max_retries"] = 10
        self.assertRejected(t, r"schema: max_retries")

    # --- semantics ---
    def test_self_review(self):
        t = load(DEV_READY)
        t["reviewer"] = t["assigned_agent"]
        self.assertRejected(t, r"reviewer must differ")

    def test_self_certified_pass(self):
        t = passed_task()
        t["quality"]["correctness"]["judged_by"] = "claude-code"
        self.assertRejected(t, r"self-report is not a pass")

    # --- 0.2: hashes and independence from run records ---
    def test_hash_mismatch(self):
        t = passed_task()
        next(e for e in t["evidence"] if e["id"] == "E1")["sha256"] = "0" * 64
        self.assertRejected(t, r"does not match the recorded hash")

    def test_hashed_evidence_file_missing(self):
        t = passed_task()
        next(e for e in t["evidence"] if e["id"] == "E1")["path"] = "runs/none/missing.log"
        self.assertRejected(t, r"does not exist")

    def test_pass_on_unhashed_evidence(self):
        t = passed_task()
        del next(e for e in t["evidence"] if e["id"] == "E1")["sha256"]
        self.assertRejected(t, r"has no verified path \+ sha256")

    def test_hash_without_path(self):
        t = load(DEV_READY)
        t["evidence"] = [{"id": "E1", "kind": "log", "sha256": "0" * 64, "produced_by": "human",
                          "created_at": "2026-10-10T01:00:00Z", "criterion_ids": []}]
        self.assertRejected(t, r"'path' is a required property")

    def test_runner_cannot_be_assigned(self):
        t = load(DEV_READY)
        t["assigned_agent"] = "aiwos-runner"
        self.assertRejected(t, r"schema: assigned_agent")

    def test_pass_without_reviewer_run(self):
        t = passed_task()
        t["evidence"] = [e for e in t["evidence"] if e["id"] != "R1"]
        t["quality"]["completeness"]["evidence_ids"] = ["E2"]
        self.assertRejected(t, r"no completed reviewer run by 'codex-cli'")

    def test_review_of_a_different_commit(self):
        t = passed_task(lambda r: r["reviewer"].update(target_commit="d" * 40))
        self.assertRejected(t, r"on the worker's result commit")

    def test_same_session_for_worker_and_reviewer(self):
        t = passed_task(lambda r: r["reviewer"].update(session_id_reported="s-worker"))
        self.assertRejected(t, r"same session id")

    def test_unreported_session_is_unproven(self):
        t = passed_task(lambda r: r["reviewer"].update(session_id_reported=None))
        self.assertRejected(t, r"independence is unproven")

    def test_same_executable_under_two_labels(self):
        t = passed_task(lambda r: r["reviewer"]["cli"].update(path="/usr/bin/claude"))
        self.assertRejected(t, r"do not name two different executables")

    def test_reviewer_ran_wrong_cli(self):
        t = passed_task(lambda r: r["reviewer"]["cli"].update(name="claude"))
        self.assertRejected(t, r"ran 'claude', not codex")

    def test_reviewer_modified_tree(self):
        t = passed_task(lambda r: r["reviewer"]["security"].update(workspace_modified_by_reviewer=True))
        self.assertRejected(t, r"did not prove it left the reviewed tree unchanged")

    def test_pass_without_check_run(self):
        t = passed_task(lambda r: r["check"].update(target_commit="d" * 40))
        self.assertRejected(t, r"no completed check run")

    def test_malformed_run_record(self):
        t = passed_task(lambda r: r["worker"].pop("auth"))
        self.assertRejected(t, r"run record 'W1' is malformed")

    def test_human_reviewer_needs_human_note(self):
        t = passed_task()
        t["reviewer"] = "human"
        self.assertRejected(t, r"no review_note produced_by 'human'")

    def test_passed_with_unverified_dimension(self):
        t = passed_task()
        t["quality"]["completeness"] = {"verdict": "UNVERIFIED", "evidence_ids": [], "judged_by": None}
        self.assertRejected(t, r"requires quality.completeness = PASS")

    def test_passed_with_uncovered_criterion(self):
        t = passed_task()
        next(e for e in t["evidence"] if e["id"] == "E2")["criterion_ids"] = []
        self.assertRejected(t, r"criterion 'AC3' has no evidence")

    def test_dangling_evidence_reference(self):
        t = passed_task()
        t["quality"]["correctness"]["evidence_ids"] = ["E9"]
        self.assertRejected(t, r"unknown evidence 'E9'")

    def test_evidence_for_unknown_criterion(self):
        t = passed_task()
        next(e for e in t["evidence"] if e["id"] == "E1")["criterion_ids"].append("AC9")
        self.assertRejected(t, r"unknown criterion 'AC9'")

    def test_duplicate_ids(self):
        t = load(DEV_READY)
        t["failure_conditions"][0]["id"] = "AC1"
        self.assertRejected(t, r"duplicate id 'AC1'")

    def test_retry_over_limit(self):
        t = load(DEV_READY)
        t["retry_count"] = 3
        self.assertRejected(t, r"exceeds max_retries")

    def test_retry_count_disagrees_with_history(self):
        t = load(DEV_READY)
        t["retry_count"] = 1
        self.assertRejected(t, r"history records 0 rework")

    def test_missing_profile(self):
        t = load(DEV_READY)
        t["task_type"] = "legal-review"
        self.assertRejected(t, r"has no profile")

    def test_missing_input_file(self):
        t = load(DEV_READY)
        t["inputs"][0]["location"] = "examples/development/src/missing.py"
        self.assertRejected(t, r"does not exist")

    def test_input_outside_repo(self):
        t = load(DEV_READY)
        t["inputs"][0]["location"] = "../etc/passwd"
        self.assertRejected(t, r"must be repo-relative")

    def test_confidential_input_stored_in_repo(self):
        t = load(DEV_READY)
        t["inputs"][0]["sensitivity"] = "confidential"
        self.assertRejected(t, r"must be kind 'reference'")

    def test_secret_in_contract(self):
        t = load(DEV_READY)
        t["constraints"].append("token: " + "ghp_" + "A" * 36)  # built at runtime, never committed
        self.assertRejected(t, r"github-token")

    def test_illegal_transition(self):
        t = load(DEV_READY)
        t["status"] = "passed"
        t["history"].append({"from": "ready", "to": "passed", "at": "2026-10-10T00:10:00Z", "by": "codex-cli"})
        self.assertRejected(t, r"ready -> passed is not allowed")

    def test_ai_cannot_accept(self):
        t = passed_task()
        t["status"] = "accepted"
        t["history"].append({"from": "passed", "to": "accepted", "at": "2026-10-10T02:00:00Z", "by": "codex-cli"})
        self.assertRejected(t, r"only a human may move a task to 'accepted'")

    def test_status_disagrees_with_history(self):
        t = load(DEV_READY)
        t["status"] = "in_progress"
        self.assertRejected(t, r"history ends in 'ready'")

    def test_non_draft_without_history(self):
        t = load(DEV_READY)
        t["history"] = []
        self.assertRejected(t, r"only 'draft' may have no history")

    def test_resume_to_different_state(self):
        t = load(DEV_READY)
        t["status"] = "review"
        t["history"] += [
            {"from": "ready", "to": "suspended", "at": "2026-10-10T00:10:00Z", "by": "human"},
            {"from": "suspended", "to": "review", "at": "2026-10-10T00:20:00Z", "by": "claude-code"},
        ]
        self.assertRejected(t, r"resume must return to 'ready'")

    def test_timestamp_backwards(self):
        t = load(DEV_READY)
        t["status"] = "in_progress"
        t["history"].append({"from": "ready", "to": "in_progress", "at": "2026-10-09T00:00:00Z", "by": "claude-code"})
        self.assertRejected(t, r"timestamp goes backwards")

    def test_timestamp_without_timezone(self):
        t = load(DEV_READY)
        t["history"][0]["at"] = "2026-10-10T00:00:00"
        self.assertRejected(t, r"date-time with timezone")


class StateMachineDocument(unittest.TestCase):
    """core/state-machine.md and TRANSITIONS must describe the same machine."""

    def test_document_matches_code(self):
        text = (ROOT / "core" / "state-machine.md").read_text(encoding="utf-8")
        documented = {}
        for m in re.finditer(r"^\| `(\w+)` \| ([^|]*) \|", text, re.M):
            documented[m.group(1)] = set(re.findall(r"`(\w+)`", m.group(2)))
        self.assertEqual(documented, vt.TRANSITIONS)

    def test_statuses_match_schema(self):
        schema = load("contracts/task.schema.json")
        self.assertEqual(set(schema["$defs"]["status"]["enum"]), set(vt.TRANSITIONS))


class CommandLine(unittest.TestCase):
    def run_cli(self, *files):
        return subprocess.run([sys.executable, str(SCRIPT), *files], cwd=ROOT,
                              capture_output=True, text=True)

    def test_valid_files_exit_zero(self):
        r = self.run_cli("examples/development/task.json", "examples/document-compare/task.json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("2 valid, 0 invalid", r.stdout)

    def test_invalid_file_exit_one(self):
        r = self.run_cli("examples/development/task.json", "tests/fixtures/invalid-task.json")
        self.assertEqual(r.returncode, 1)
        self.assertIn("1 valid, 1 invalid", r.stdout)

    def test_unreadable_file(self):
        r = self.run_cli("does-not-exist.json")
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot read JSON", r.stdout)


if __name__ == "__main__":
    unittest.main()
