#!/usr/bin/env python3
"""Validate AI WORK OS task contracts.

Usage:
  python3 scripts/validate-task.py TASK.json [TASK.json ...]

Two layers run on every file:
  1. structure  - contracts/task.schema.json (JSON Schema 2020-12)
  2. semantics  - the cross-field rules in core/contract.md, which a schema
                  cannot express (reviewer independence, evidence references,
                  state transitions, retry limit, secrets, input files).

Exit 0 when every file is valid, 1 when any is invalid, 2 on usage or
environment errors (for example jsonschema not installed).
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "contracts" / "task.schema.json"
PROFILES_DIR = ROOT / "profiles"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scan_secrets import find_in_text  # noqa: E402

# Allowed status transitions. core/state-machine.md documents the same table;
# tests/test_validate_task.py fails if the two drift apart.
TRANSITIONS = {
    "draft": {"ready", "cancelled"},
    "ready": {"in_progress", "suspended", "cancelled"},
    "in_progress": {"review", "escalated", "suspended", "cancelled"},
    "review": {"passed", "rework", "escalated", "suspended", "cancelled"},
    "rework": {"in_progress", "escalated", "suspended", "cancelled"},
    "passed": {"accepted", "rework", "escalated"},
    "escalated": {"ready", "cancelled"},
    "suspended": {"ready", "in_progress", "review", "rework", "cancelled"},
    "accepted": set(),
    "cancelled": set(),
}
DIMENSIONS = ("spec_conformance", "correctness", "completeness")
HUMAN_ONLY_TARGETS = {"accepted"}


def load_schema_validator():
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        print("validate-task: python package 'jsonschema' is required "
              "(pip install -r requirements.txt)", file=sys.stderr)
        sys.exit(2)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def structural_errors(validator, task):
    errors = []
    for err in sorted(validator.iter_errors(task), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"schema: {where}: {err.message}")
    return errors


def parse_datetime(value):
    """Timezone-aware datetime, or None when value is not an ISO 8601 date-time with offset."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return parsed if parsed.tzinfo else None


def semantic_errors(task):
    """Cross-field rules. Assumes the structure already validated."""
    errors = []
    agent = task["assigned_agent"]
    status = task["status"]

    # Independence: the implementer never reviews its own work.
    if task["reviewer"] == agent:
        errors.append(f"reviewer must differ from assigned_agent (both are '{agent}')")

    # Profile must exist so business-specific rules are actually defined.
    if not (PROFILES_DIR / f"{task['task_type']}.md").is_file():
        errors.append(f"task_type '{task['task_type']}' has no profile at profiles/{task['task_type']}.md")

    # Ids are unique within the task.
    seen = {}
    for section in ("inputs", "acceptance_criteria", "failure_conditions", "evidence"):
        for item in task[section]:
            if item["id"] in seen:
                errors.append(f"duplicate id '{item['id']}' in {section} (already used in {seen[item['id']]})")
            seen[item["id"]] = section
    criteria = {c["id"]: c for c in task["acceptance_criteria"]}
    evidence = {e["id"]: e for e in task["evidence"]}

    # Inputs: files exist, confidential data is only referenced.
    for inp in task["inputs"]:
        if inp["sensitivity"] == "confidential" and inp["kind"] != "reference":
            errors.append(f"input '{inp['id']}' is confidential: it must be kind 'reference', "
                          "never stored in the repository")
        if inp["kind"] == "file":
            path = Path(inp["location"])
            if path.is_absolute() or ".." in path.parts:
                errors.append(f"input '{inp['id']}': file location must be repo-relative")
            elif not (ROOT / path).is_file():
                errors.append(f"input '{inp['id']}': file '{inp['location']}' does not exist")

    # Evidence: references resolve, timestamps parse.
    for ev in task["evidence"]:
        for cid in ev["criterion_ids"]:
            if cid not in criteria:
                errors.append(f"evidence '{ev['id']}' refers to unknown criterion '{cid}'")
        if parse_datetime(ev["created_at"]) is None:
            errors.append(f"evidence '{ev['id']}': created_at is not an ISO 8601 date-time with timezone")

    # Quality: three separate judgments, never self-certified, never without evidence.
    for dim in DIMENSIONS:
        j = task["quality"][dim]
        for eid in j["evidence_ids"]:
            if eid not in evidence:
                errors.append(f"quality.{dim} refers to unknown evidence '{eid}'")
        if j["verdict"] == "PASS" and j["judged_by"] == agent:
            errors.append(f"quality.{dim}: PASS judged by the assigned agent itself; "
                          "a self-report is not a pass")

    # Passing the gate needs all three PASS and evidence for every criterion.
    if status in ("passed", "accepted"):
        for dim in DIMENSIONS:
            if task["quality"][dim]["verdict"] != "PASS":
                errors.append(f"status '{status}' requires quality.{dim} = PASS "
                              f"(is {task['quality'][dim]['verdict']})")
        covered = {cid for ev in task["evidence"] for cid in ev["criterion_ids"]}
        for cid in criteria:
            if cid not in covered:
                errors.append(f"status '{status}' but acceptance criterion '{cid}' has no evidence")

    # Retry limit.
    if task["retry_count"] > task["max_retries"]:
        errors.append(f"retry_count {task['retry_count']} exceeds max_retries {task['max_retries']}")

    errors.extend(history_errors(task))

    # Secrets never go into a contract.
    for lineno, name in find_in_text(json.dumps(task, ensure_ascii=False, indent=1)):
        errors.append(f"value matching '{name}' found; credentials must not appear in a task contract")

    return errors


def history_errors(task):
    errors = []
    history = task["history"]
    status = task["status"]
    if not history:
        if status != "draft":
            errors.append(f"status is '{status}' but history is empty; only 'draft' may have no history")
        if task["retry_count"]:
            errors.append("retry_count is non-zero but history records no rework")
        return errors

    if history[0]["from"] != "draft":
        errors.append(f"history must start from 'draft' (starts from '{history[0]['from']}')")
    before_suspend = None
    prev_at = None
    for i, step in enumerate(history):
        src, dst = step["from"], step["to"]
        if i and src != history[i - 1]["to"]:
            errors.append(f"history[{i}]: from '{src}' but previous step ended in '{history[i - 1]['to']}'")
        if dst not in TRANSITIONS[src]:
            errors.append(f"history[{i}]: transition {src} -> {dst} is not allowed")
        if dst in HUMAN_ONLY_TARGETS and step["by"] != "human":
            errors.append(f"history[{i}]: only a human may move a task to '{dst}' (by '{step['by']}')")
        if dst == "suspended":
            before_suspend = src
        if src == "suspended" and dst != "cancelled" and dst != before_suspend:
            errors.append(f"history[{i}]: resume must return to '{before_suspend}', not '{dst}'")
        at = parse_datetime(step["at"])
        if at is None:
            errors.append(f"history[{i}]: 'at' is not an ISO 8601 date-time with timezone")
        elif prev_at and at < prev_at:
            errors.append(f"history[{i}]: timestamp goes backwards")
        else:
            prev_at = at

    if history[-1]["to"] != status:
        errors.append(f"status is '{status}' but history ends in '{history[-1]['to']}'")
    reworks = sum(1 for s in history if s["to"] == "rework")
    if reworks != task["retry_count"]:
        errors.append(f"retry_count is {task['retry_count']} but history records {reworks} rework(s)")
    return errors


def validate_task(task, validator):
    """Return a list of error strings; empty means valid."""
    errors = structural_errors(validator, task)
    if errors:
        return errors  # semantic rules assume a well-formed task
    return semantic_errors(task)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    validator = load_schema_validator()
    invalid = 0
    for name in argv:
        path = Path(name)
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"INVALID {path}\n  - cannot read JSON: {exc}")
            invalid += 1
            continue
        errors = validate_task(task, validator)
        if errors:
            invalid += 1
            print(f"INVALID {path}")
            for e in errors:
                print(f"  - {e}")
        else:
            print(f"OK      {path} ({task['task_id']}, status={task['status']})")
    print(f"validate-task: {len(argv) - invalid} valid, {invalid} invalid")
    return 1 if invalid else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
