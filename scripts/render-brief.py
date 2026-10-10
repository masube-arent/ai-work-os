#!/usr/bin/env python3
"""Render a task contract as a brief to hand to an agent by hand.

Usage:
  python3 scripts/render-brief.py TASK.json [--role worker|reviewer]

Prints Markdown to stdout and does nothing else: it starts no AI, calls no
CLI and sends nothing anywhere. Paste the output into Claude Code, Codex CLI,
ChatGPT or give it to a person. The task is validated first; an invalid task
gets no brief.
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("validate_task", HERE / "validate-task.py")
vt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vt)

ROLE_RULES = {
    "worker": [
        "受入基準と制約の範囲だけを作業する。基準を自分で変えない。",
        "完了の申告は合格ではない。各受入基準の検証コマンドを実行し、コマンドと終了コードを証拠として報告する。",
        "失敗条件に該当したら作業を止め、該当した条件 ID を報告する。",
        "秘密情報をコード・ログ・報告に書かない。",
    ],
    "reviewer": [
        "実装者の報告を信じず、検証コマンドを自分で実行する。",
        "3つの品質軸（spec_conformance / correctness / completeness）を別々に判定する。",
        "証拠のない項目は PASS にせず UNVERIFIED とする。",
        "判定ごとに根拠となる証拠（コマンド・終了コード・ファイル）を示す。",
    ],
}


def render(task, role):
    who = task["assigned_agent"] if role == "worker" else task["reviewer"]
    out = [f"# {task['task_id']} — {role} brief (for: {who})", "",
           f"- task_type: `{task['task_type']}` (profiles/{task['task_type']}.md)",
           f"- status: `{task['status']}`, retry {task['retry_count']}/{task['max_retries']}", "",
           "## 目的", task["objective"], "", "## 入力"]
    for i in task["inputs"] or []:
        out.append(f"- `{i['id']}` [{i['kind']}, {i['sensitivity']}] {i['location']}"
                   + (f" — {i['description']}" if i.get("description") else ""))
    if not task["inputs"]:
        out.append("- （なし）")
    out += ["", "## 制約"] + [f"- {c}" for c in task["constraints"] or ["（なし）"]]
    out += ["", "## 受入基準"]
    for c in task["acceptance_criteria"]:
        v = c["verification"]
        line = f"- **{c['id']}** ({c['dimension']}) {c['description']} — 検証: {v['method']}"
        if v.get("command"):
            line += f"\n  - `{v['command']}`" + (f"（期待: {v['expected']}）" if v.get("expected") else "")
        out.append(line)
    out += ["", "## 失敗条件"]
    out += [f"- **{f['id']}** {f['description']} → {f['on_trigger']}" for f in task["failure_conditions"]]
    out += ["", "## 作業規則"] + [f"- {r}" for r in ROLE_RULES[role]]
    return "\n".join(out) + "\n"


def main(argv):
    p = argparse.ArgumentParser(description="Render a task brief (dry run, prints only).")
    p.add_argument("task")
    p.add_argument("--role", choices=sorted(ROLE_RULES), default="worker")
    args = p.parse_args(argv)
    task = json.loads(Path(args.task).read_text(encoding="utf-8"))
    errors = vt.validate_task(task, vt.load_schema_validator())
    if errors:
        print(f"render-brief: {args.task} is not a valid task; run validate-task.py", file=sys.stderr)
        return 1
    if task["status"] in ("draft", "accepted", "cancelled"):
        print(f"render-brief: status '{task['status']}' is not workable", file=sys.stderr)
        return 1
    sys.stdout.write(render(task, args.role))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
