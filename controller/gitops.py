"""Git and GitHub operations for the controller.

Tasks are discovered only from contract files on the remote base branch
(origin/main): those reached main through a pull request. Issue and PR text is
never read as an instruction. Results go back as a task branch
`aiwos/task/<task_id>` (plus the runner's `run/<task_id>/*` branches) and,
with publish=github, a pull request opened with the GitHub CLI.
"""
import fnmatch
import json
import os
import subprocess
import time

TASK_BRANCH = "aiwos/task/{}"
AUTHOR = {"GIT_AUTHOR_NAME": "aiwos-controller", "GIT_AUTHOR_EMAIL": "aiwos-controller@localhost",
          "GIT_COMMITTER_NAME": "aiwos-controller", "GIT_COMMITTER_EMAIL": "aiwos-controller@localhost"}


class GitError(RuntimeError):
    pass


class Diverged(GitError):
    """The local task branch and the remote one each have commits the other
    lacks. Nothing is overwritten; a person decides."""


def git(repo, *args, check=True, env=None, timeout=300, lock_retries=5):
    """Run git. Ten workers share one object store and ref namespace, so a
    command can lose a race for a ref or packed-refs lock; those failures are
    retried (every command used here is safe to repeat)."""
    for attempt in range(lock_retries + 1):
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout,
                             env={**os.environ, **(env or {})})
        if out.returncode == 0 or ".lock" not in out.stderr or attempt == lock_retries:
            break
        time.sleep(0.2 * (attempt + 1))
    if check and out.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])}: {out.stderr.strip()[:500]}")
    return out.stdout.strip() if check else out


def fetch(repo, remote):
    git(repo, "fetch", "--prune", remote, timeout=600)


def ref_exists(repo, ref):
    return git(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False).returncode == 0


def rev(repo, ref):
    """Commit (or blob, for `ref:path`) id, or None if it does not exist."""
    out = git(repo, "rev-parse", "--verify", "--quiet", ref, check=False)
    return out.stdout.strip() if out.returncode == 0 else None


def is_ancestor(repo, older, newer):
    return git(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def list_contracts(repo, ref, globs):
    names = git(repo, "ls-tree", "-r", "--name-only", ref).splitlines()
    return sorted(n for n in names if any(fnmatch.fnmatchcase(n, g) for g in globs))


def read_json_at(repo, ref, path):
    return json.loads(git(repo, "show", f"{ref}:{path}"))


# ---------------------------------------------------------------- worktrees

def ensure_worktree(repo, path, base_ref):
    """One worktree per worker, reused across tasks. Created by the controller,
    one at a time, before any worker starts; reset on every worker start so a
    crashed predecessor's half-written files never leak into the next task."""
    if not os.path.exists(os.path.join(path, ".git")):
        git(repo, "worktree", "prune")
        git(repo, "worktree", "add", "--detach", "--force", path, base_ref)
    reset_worktree(path)


def reset_worktree(path):
    git(path, "reset", "--hard", "--quiet")
    git(path, "clean", "-fdq")
    git(path, "checkout", "--detach", "--quiet")


def checkout_task_branch(repo, worktree, task_id, remote, base_branch):
    """Put the task branch in this worktree, taking whichever of the local and
    remote branch is newer. If GitHub is ahead (a person pushed a fix), the
    local branch is fast-forwarded to it; if the local branch is ahead
    (unpublished results), it is kept; if they have diverged, Diverged is
    raised and nothing is changed."""
    branch = TASK_BRANCH.format(task_id)
    local, upstream = rev(repo, f"refs/heads/{branch}"), rev(repo, f"refs/remotes/{remote}/{branch}")
    if local and upstream and local != upstream and not is_ancestor(repo, upstream, local):
        if not is_ancestor(repo, local, upstream):
            raise Diverged(f"{branch}: local {local[:12]} and {remote} {upstream[:12]} have diverged")
        start = f"{remote}/{branch}"  # remote is ahead: fast-forward
    elif local:
        start = branch
    elif upstream:
        start = f"{remote}/{branch}"
    else:
        start = f"{remote}/{base_branch}"
    if start == branch:
        git(worktree, "checkout", "--quiet", branch)
    else:
        # --no-track: setting an upstream writes the shared .git/config, which
        # ten workers would then fight over
        git(worktree, "checkout", "--quiet", "--no-track", "-B", branch, start)
    return branch


def commit_all(worktree, paths, message):
    git(worktree, "add", "--", *paths)
    if git(worktree, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return None
    git(worktree, "commit", "--quiet", "-m", message, env=AUTHOR)
    return git(worktree, "rev-parse", "HEAD")


# ---------------------------------------------------------------- publishing

def pr_text(task_id, contract):
    quality = contract.get("quality", {})
    lines = [f"AI WORK OS controller の作業ブランチです（タスク `{task_id}`）。",
             "", f"- 状態: `{contract.get('status')}`",
             *[f"- {dim}: {q.get('verdict')} (judged_by: {q.get('judged_by')})" for dim, q in quality.items()],
             "", "マージと `accepted` は人間が判断します。controller はマージも承認もしません。"]
    return f"aiwos: {task_id} ({contract.get('status')})", "\n".join(lines)


def unpublished(repo, remote, task_id):
    """True when the local task branch has commits the remote does not."""
    branch = TASK_BRANCH.format(task_id)
    if not ref_exists(repo, f"refs/heads/{branch}"):
        return False
    local = git(repo, "rev-parse", f"refs/heads/{branch}")
    remote_ref = f"refs/remotes/{remote}/{branch}"
    return not ref_exists(repo, remote_ref) or git(repo, "rev-parse", remote_ref) != local


def publish(repo, mode, remote, base_branch, task, title, body, env=None):
    """Push the task branch and its run branches; with mode=github also make
    sure a pull request exists. Never merges. Returns a dict for the event log."""
    if mode == "none":
        return {"published": False}
    task_id = task["task_id"]
    branch = TASK_BRANCH.format(task_id)
    refspecs = [f"refs/heads/{branch}:refs/heads/{branch}"]
    if git(repo, "for-each-ref", f"refs/heads/run/{task_id}/", "--format=%(refname)"):
        refspecs.append(f"refs/heads/run/{task_id}/*:refs/heads/run/{task_id}/*")
    git(repo, "push", "--quiet", remote, *refspecs, env=env, timeout=600)
    result = {"published": True, "branch": branch, "pushed_sha": rev(repo, f"refs/heads/{branch}")}
    if mode == "github":
        # a failed PR after a successful push is recorded, not raised: the push
        # stands, and the sync retries the PR alone (ensure_pull_request never
        # creates a second one)
        try:
            result["pr"] = ensure_pull_request(repo, base_branch, branch, title, body, env)
        except (GitError, OSError, subprocess.TimeoutExpired) as exc:
            result["pr_error"] = str(exc)[:300]
    return result


def ensure_pull_request(repo, base_branch, branch, title, body, env=None):
    """Open a PR for the task branch unless one is already open. Uses only
    `gh pr list` and `gh pr create` (checked against `gh --help`, 2.89.0)."""
    run_env = {**os.environ, **(env or {})}
    found = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url",
                            "--jq", ".[0].url"], cwd=repo, capture_output=True, text=True, env=run_env, timeout=120)
    if found.returncode != 0:
        raise GitError(f"gh pr list failed: {found.stderr.strip()[:300]}")
    if found.stdout.strip():
        return found.stdout.strip()
    made = subprocess.run(["gh", "pr", "create", "--base", base_branch, "--head", branch, "--title", title,
                           "--body", body], cwd=repo, capture_output=True, text=True, env=run_env, timeout=120)
    if made.returncode != 0:
        raise GitError(f"gh pr create failed: {made.stderr.strip()[:300]}")
    return made.stdout.strip().splitlines()[-1] if made.stdout.strip() else None
