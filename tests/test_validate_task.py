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


def passed_task():
    """DEV-0001 carried through to 'passed' with evidence for every criterion.

    Test data only: the evidence entries describe a hypothetical run and are
    never written to examples/.
    """
    t = load("examples/development/task.json")
    t["status"] = "passed"
    t["evidence"] = [
        {"id": "E1", "kind": "command_output", "command": "python3 -m unittest -v", "exit_code": 0,
         "produced_by": "codex-cli", "created_at": "2026-10-10T01:00:00Z", "criterion_ids": ["AC1", "AC2"]},
        {"id": "E2", "kind": "review_note", "summary": "tests added, unsupported lang documented",
         "produced_by": "codex-cli", "created_at": "2026-10-10T01:05:00Z", "criterion_ids": ["AC3"]},
    ]
    t["quality"] = {
        "spec_conformance": {"verdict": "PASS", "evidence_ids": ["E1"], "judged_by": "codex-cli"},
        "correctness": {"verdict": "PASS", "evidence_ids": ["E1"], "judged_by": "codex-cli"},
        "completeness": {"verdict": "PASS", "evidence_ids": ["E2"], "judged_by": "codex-cli"},
    }
    t["history"] += [
        {"from": "ready", "to": "in_progress", "at": "2026-10-10T00:10:00Z", "by": "claude-code"},
        {"from": "in_progress", "to": "review", "at": "2026-10-10T00:50:00Z", "by": "claude-code"},
        {"from": "review", "to": "passed", "at": "2026-10-10T01:10:00Z", "by": "codex-cli"},
    ]
    return t


class ValidTasks(unittest.TestCase):
    def assertValid(self, task):
        self.assertEqual(vt.validate_task(task, VALIDATOR), [])

    def test_development_sample(self):
        self.assertValid(load("examples/development/task.json"))

    def test_document_compare_sample(self):
        self.assertValid(load("examples/document-compare/task.json"))

    def test_template(self):
        self.assertValid(load("contracts/task.example.json"))

    def test_both_samples_share_one_contract(self):
        dev = load("examples/development/task.json")
        doc = load("examples/document-compare/task.json")
        self.assertEqual(set(dev), set(doc))
        self.assertNotEqual(dev["task_type"], doc["task_type"])

    def test_fully_evidenced_pass(self):
        self.assertValid(passed_task())

    def test_rework_cycle_counts_retry(self):
        t = load("examples/development/task.json")
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
        t = load("examples/development/task.json")
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
        t = load("examples/development/task.json")
        del t["objective"]
        self.assertRejected(t, r"'objective' is a required property")

    def test_unknown_field(self):
        t = load("examples/development/task.json")
        t["priority"] = "high"
        self.assertRejected(t, r"Additional properties")

    def test_unknown_status(self):
        t = load("examples/development/task.json")
        t["status"] = "done"
        self.assertRejected(t, r"schema: status")

    def test_unknown_agent(self):
        t = load("examples/development/task.json")
        t["assigned_agent"] = "some-new-model"
        self.assertRejected(t, r"schema: assigned_agent")

    def test_no_acceptance_criteria(self):
        t = load("examples/development/task.json")
        t["acceptance_criteria"] = []
        self.assertRejected(t, r"schema: acceptance_criteria")

    def test_command_criterion_without_command(self):
        t = load("examples/development/task.json")
        del t["acceptance_criteria"][0]["verification"]["command"]
        self.assertRejected(t, r"'command' is a required property")

    def test_pass_without_evidence(self):
        t = load("examples/development/task.json")
        t["quality"]["correctness"] = {"verdict": "PASS", "evidence_ids": [], "judged_by": "codex-cli"}
        self.assertRejected(t, r"quality/correctness/evidence_ids")

    def test_escalated_without_reason(self):
        t = load("examples/development/task.json")
        t["status"] = "escalated"
        self.assertRejected(t, r"schema: escalation_reason")

    def test_retry_cap_above_policy(self):
        t = load("examples/development/task.json")
        t["max_retries"] = 10
        self.assertRejected(t, r"schema: max_retries")

    # --- semantics ---
    def test_self_review(self):
        t = load("examples/development/task.json")
        t["reviewer"] = t["assigned_agent"]
        self.assertRejected(t, r"reviewer must differ")

    def test_self_certified_pass(self):
        t = passed_task()
        t["quality"]["correctness"]["judged_by"] = "claude-code"
        self.assertRejected(t, r"self-report is not a pass")

    def test_passed_with_unverified_dimension(self):
        t = passed_task()
        t["quality"]["completeness"] = {"verdict": "UNVERIFIED", "evidence_ids": [], "judged_by": None}
        self.assertRejected(t, r"requires quality.completeness = PASS")

    def test_passed_with_uncovered_criterion(self):
        t = passed_task()
        t["evidence"][1]["criterion_ids"] = []
        self.assertRejected(t, r"criterion 'AC3' has no evidence")

    def test_dangling_evidence_reference(self):
        t = passed_task()
        t["quality"]["correctness"]["evidence_ids"] = ["E9"]
        self.assertRejected(t, r"unknown evidence 'E9'")

    def test_evidence_for_unknown_criterion(self):
        t = passed_task()
        t["evidence"][0]["criterion_ids"].append("AC9")
        self.assertRejected(t, r"unknown criterion 'AC9'")

    def test_duplicate_ids(self):
        t = load("examples/development/task.json")
        t["failure_conditions"][0]["id"] = "AC1"
        self.assertRejected(t, r"duplicate id 'AC1'")

    def test_retry_over_limit(self):
        t = load("examples/development/task.json")
        t["retry_count"] = 3
        self.assertRejected(t, r"exceeds max_retries")

    def test_retry_count_disagrees_with_history(self):
        t = load("examples/development/task.json")
        t["retry_count"] = 1
        self.assertRejected(t, r"history records 0 rework")

    def test_missing_profile(self):
        t = load("examples/development/task.json")
        t["task_type"] = "legal-review"
        self.assertRejected(t, r"has no profile")

    def test_missing_input_file(self):
        t = load("examples/development/task.json")
        t["inputs"][0]["location"] = "examples/development/src/missing.py"
        self.assertRejected(t, r"does not exist")

    def test_input_outside_repo(self):
        t = load("examples/development/task.json")
        t["inputs"][0]["location"] = "../etc/passwd"
        self.assertRejected(t, r"must be repo-relative")

    def test_confidential_input_stored_in_repo(self):
        t = load("examples/development/task.json")
        t["inputs"][0]["sensitivity"] = "confidential"
        self.assertRejected(t, r"must be kind 'reference'")

    def test_secret_in_contract(self):
        t = load("examples/development/task.json")
        t["constraints"].append("token: " + "ghp_" + "A" * 36)  # built at runtime, never committed
        self.assertRejected(t, r"github-token")

    def test_illegal_transition(self):
        t = load("examples/development/task.json")
        t["status"] = "passed"
        t["history"].append({"from": "ready", "to": "passed", "at": "2026-10-10T00:10:00Z", "by": "codex-cli"})
        self.assertRejected(t, r"ready -> passed is not allowed")

    def test_ai_cannot_accept(self):
        t = passed_task()
        t["status"] = "accepted"
        t["history"].append({"from": "passed", "to": "accepted", "at": "2026-10-10T02:00:00Z", "by": "codex-cli"})
        self.assertRejected(t, r"only a human may move a task to 'accepted'")

    def test_status_disagrees_with_history(self):
        t = load("examples/development/task.json")
        t["status"] = "in_progress"
        self.assertRejected(t, r"history ends in 'ready'")

    def test_non_draft_without_history(self):
        t = load("examples/development/task.json")
        t["history"] = []
        self.assertRejected(t, r"only 'draft' may have no history")

    def test_resume_to_different_state(self):
        t = load("examples/development/task.json")
        t["status"] = "review"
        t["history"] += [
            {"from": "ready", "to": "suspended", "at": "2026-10-10T00:10:00Z", "by": "human"},
            {"from": "suspended", "to": "review", "at": "2026-10-10T00:20:00Z", "by": "claude-code"},
        ]
        self.assertRejected(t, r"resume must return to 'ready'")

    def test_timestamp_backwards(self):
        t = load("examples/development/task.json")
        t["status"] = "in_progress"
        t["history"].append({"from": "ready", "to": "in_progress", "at": "2026-10-09T00:00:00Z", "by": "claude-code"})
        self.assertRejected(t, r"timestamp goes backwards")

    def test_timestamp_without_timezone(self):
        t = load("examples/development/task.json")
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
