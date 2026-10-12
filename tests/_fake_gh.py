#!/usr/bin/env python3
"""A fake `gh` for tests: implements just the REST calls ensure_pull_request
makes — `gh api -X GET repos/<owner>/<repo>/pulls ...` and `-X POST ...` — and
nothing else. It never contacts GitHub.

State lives in the directory named by FAKE_GH_STATE:
  calls.jsonl   one JSON line per invocation (the argv)
  prs.json      {branch: html_url} for PRs it has "created" (dedup source)
  fail_post     if this file exists, POST fails with a 502 (to test retry)

A PR's head is `owner:branch` on GET and `branch` on POST; both are keyed by
the branch alone so GET after POST finds it (dedup).
"""
import json
import os
import sys


def field(args, name):
    for i, a in enumerate(args):
        if a in ("-f", "-F", "--raw-field", "--field") and i + 1 < len(args):
            k, _, v = args[i + 1].partition("=")
            if k == name:
                return v
    return None


def branch_of(head):
    return head.split(":", 1)[1] if head and ":" in head else head


def main(argv):
    state = os.environ["FAKE_GH_STATE"]
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "calls.jsonl"), "a") as f:
        f.write(json.dumps(argv) + "\n")
    if argv[:1] != ["api"] or "-X" not in argv:
        print(f"fake gh: unsupported call {argv}", file=sys.stderr)
        return 2
    method = argv[argv.index("-X") + 1]
    if not any(a.endswith("/pulls") for a in argv):
        print("fake gh: only /pulls is supported", file=sys.stderr)
        return 2
    prs_path = os.path.join(state, "prs.json")
    prs = json.load(open(prs_path)) if os.path.exists(prs_path) else {}
    if method == "GET":
        print(prs.get(branch_of(field(argv, "head")), ""))
        return 0
    if method == "POST":
        if os.path.exists(os.path.join(state, "fail_post")):
            print("HTTP 502: GitHub had a bad moment", file=sys.stderr)
            return 1
        branch = branch_of(field(argv, "head"))
        url = prs.get(branch) or f"https://github.com/example/repo/pull/{len(prs) + 1}"
        prs[branch] = url
        json.dump(prs, open(prs_path, "w"))
        print(url)
        return 0
    print(f"fake gh: unsupported method {method}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
