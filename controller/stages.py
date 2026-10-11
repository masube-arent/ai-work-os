"""What a task needs next, decided only from its contract and run records.

The same function serves the sync (reading files at origin/main or the task
branch) and the workers (reading their worktree), through `read(path)`.

Stages map one-to-one onto existing scripts/aiwos.py commands:
  work    run --role worker --execute      (model)
  check   check --execute                  (no model)
  review  run --role reviewer --execute    (model)
  judge   judge --write                    (no model)
  resume  transition suspended -> in_progress, only after the runner's own interrupt
Anything a machine may not decide (passed, escalated, a human suspension) is held.
"""
import json

RUNNABLE_AGENTS = {"claude-code", "codex-cli"}
HUMAN_WAIT = "waiting for a human: the controller never grants 'accepted' (Security Fix 001)"


def _runs(contract, read):
    out = []
    for ev in contract.get("evidence", []):
        if ev.get("kind") == "run_record" and ev.get("path"):
            try:
                out.append(json.loads(read(ev["path"])))
            except (OSError, ValueError, KeyError):
                continue
    return out


def _latest(runs, role, target=None):
    found = [r for r in runs if r.get("role") == role and r.get("status") == "completed"
             and (target is None or r.get("target_commit") == target)]
    return found[-1] if found else None


def next_stage(contract, read, after=None):
    """Return (stage, agent, state, reason). state is queued, held or done."""
    status = contract.get("status")
    if status in ("accepted", "cancelled"):
        return None, None, "done", status
    if status == "draft":
        return None, None, "held", "draft: acceptance criteria not fixed yet"
    if status == "passed":
        return None, None, "held", HUMAN_WAIT
    if status == "escalated":
        return None, None, "held", f"escalated: {contract.get('escalation_reason')}"
    if status == "suspended":
        last = (contract.get("history") or [{}])[-1]
        if last.get("by") == "aiwos-runner" and "interrupted" in (last.get("note") or ""):
            return "resume", None, "queued", "resume after the runner's own interrupt"
        return None, None, "held", "suspended by someone other than the runner"
    if status in ("ready", "rework", "in_progress"):
        agent = contract.get("assigned_agent")
        if agent not in RUNNABLE_AGENTS:
            return None, None, "held", f"assigned_agent '{agent}' has no CLI adapter"
        return "work", agent, "queued", None
    if status == "review":
        if after == "judge":
            return None, None, "held", "judge left a dimension UNVERIFIED; needs a human or another review"
        runs = _runs(contract, read)
        worker = _latest(runs, "worker")
        if worker is None:
            return None, None, "held", "status review without a completed worker run"
        head = worker.get("result_commit")
        has_command = any(c.get("verification", {}).get("method") == "command"
                          for c in contract.get("acceptance_criteria", []))
        if has_command and _latest(runs, "check", head) is None:
            return "check", None, "queued", None
        if _latest(runs, "reviewer", head) is None:
            reviewer = contract.get("reviewer")
            if reviewer not in RUNNABLE_AGENTS:
                return None, None, "held", f"reviewer '{reviewer}' has no CLI adapter"
            return "review", reviewer, "queued", None
        return "judge", None, "queued", None
    return None, None, "held", f"unknown status '{status}'"


def command(stage, contract_path, timeout):
    """argv tail for scripts/aiwos.py."""
    return {
        "work": ["run", contract_path, "--role", "worker", "--execute", "--timeout", str(timeout)],
        "check": ["check", contract_path, "--execute", "--timeout", str(timeout)],
        "review": ["run", contract_path, "--role", "reviewer", "--execute", "--timeout", str(timeout)],
        "judge": ["judge", contract_path, "--write"],
        "resume": ["transition", contract_path, "--to", "in_progress", "--by", "aiwos-runner",
                   "--reason", "controller: resume after the runner's own interrupt"],
    }[stage]
