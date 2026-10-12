"""When the trusted side may run AI and may publish (Security Fix 002).

Two gates, both fail closed:

execution_allowed — may an untrusted stage (an AI CLI, or a command an AI
  wrote) run at all? If a GitHub credential is readable by the OS user the
  agent runs as, running the AI would expose it (env vars are scrubbed, but a
  same-user process still reads /proc and credential files), so AI is run only
  when isolation is verified OR no credential is present at all. A stub test
  carrying no credential keeps running without isolation. With
  require_isolation (the local CLI's setting) only a verified isolation will do.
  Under WSL the isolation also needs Windows interop and drive mounts off
  (controller/wsl.py).

publish_allowed — may results be pushed?
  - publish=none                      never
  - publish=git, every push URL local allowed (nothing leaves the machine)
  - a network push URL, or publish=github (which uses GH_TOKEN): only while a
    recent agent-user probe proved it cannot read the token, the trusted
    processes' /proc, the configured credential files, or write trusted code,
    state or the repository
"""
import os
import time
from pathlib import Path

from . import gitops, wsl

ISOLATION_KEY = "isolation"
# A GitHub credential in the controller/worker environment that a same-user
# agent could read. Credential files are named in cfg["credential_files"].
TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")


def isolation_verified(cfg, store):
    verdict, checked_at = store.get_meta(ISOLATION_KEY)
    if not verdict or not verdict.get("ok"):
        return False, (verdict or {}).get("reason", "isolation never verified")
    if time.time() - checked_at > cfg["isolation_check_ttl_s"]:
        return False, "isolation check is stale"
    return True, "isolation verified"


def credentials_exposed(cfg):
    """A credential a same-OS-user agent could read: a token in this process's
    environment, or a configured credential file that exists. Returns (bool,
    what) — `what` never contains the credential value, only where it is."""
    present = [name for name in TOKEN_ENV if os.environ.get(name)]
    files = [f for f in cfg.get("credential_files", []) if Path(f).exists()]
    if present or files:
        return True, {"env": present, "files": files}
    return False, {}


def execution_allowed(cfg, store):
    """May an untrusted stage run? Isolated: yes. Not isolated but no credential
    anywhere: yes (stub tests). Not isolated with a credential present: no."""
    ok, why = isolation_verified(cfg, store)
    if ok:
        return True, why
    if cfg.get("require_isolation"):
        return False, f"AI execution disabled (fail closed): require_isolation is set and {why}"
    exposed, what = credentials_exposed(cfg)
    if not exposed:
        return True, "no credentials present; AI may run unisolated"
    return False, (f"AI execution disabled (fail closed): a GitHub credential is readable by the agent's OS "
                   f"user and isolation is not verified ({why}); exposed: {what}")


def publish_allowed(cfg, store):
    if cfg["publish"] == "none":
        return False, "publish=none"
    if cfg["publish"] == "git":
        is_local, urls = gitops.remote_is_local(cfg["repo_dir"], cfg["remote"])
        if is_local:
            return True, f"local remote ({', '.join(urls)})"
        # a git remote whose effective push target is network (pushurl, extra
        # URL, or insteadOf/pushInsteadOf rewrite) needs isolation like github
    ok, why = isolation_verified(cfg, store)
    if not ok:
        return False, f"publishing disabled (fail closed): {why}"
    return True, "isolation verified"


def check_isolation(cfg, store):
    """Run the probe as the agent user and store the verdict."""
    import isolation  # scripts/ is on sys.path in the controller process
    state = Path(cfg["state_dir"])
    canary = state / "isolation-canary"
    if not canary.exists():
        canary.write_text("trusted-side canary; the agent user must not be able to read this\n")
    os.chmod(canary, 0o600)
    home = Path(os.path.expanduser("~"))
    read_files = [canary, state / "aiwos.db", *cfg["credential_files"]]
    write_dirs = [cfg["code_dir"], Path(cfg["code_dir"]) / "scripts", Path(cfg["code_dir"]) / "controller",
                  state, cfg["repo_dir"], Path(cfg["repo_dir"]) / ".git", home]
    missing = [str(p) for p in cfg["credential_files"] if not Path(p).exists()]
    ok, report = isolation.probe(read_files, write_dirs, [os.getpid(), os.getppid()])
    reason = None
    if not ok:
        reason = report.get("error") or (f"agent user could: {report['allowed']}" if report.get("allowed")
                                         else "probe ran as the trusted user" if report.get("same_user")
                                         else "probe failed")
    try:
        hardlinks = Path("/proc/sys/fs/protected_hardlinks").read_text().strip()
    except OSError:
        hardlinks = None
    if ok and hardlinks != "1":
        ok, reason = False, f"fs.protected_hardlinks is {hardlinks}, must be 1"
    if ok and missing:
        ok, reason = False, f"configured credential_files missing (cannot prove they are protected): {missing}"
    if ok and wsl.is_wsl():
        holes = wsl.problems()
        if holes:
            ok, reason = False, "WSL: " + "; ".join(holes)
    verdict = {"ok": ok, "reason": reason, "uid": report.get("uid"), "allowed": report.get("allowed", []),
               "results": report.get("results", {})}
    store.set_meta(ISOLATION_KEY, verdict)
    store.event("isolation_checked", detail={"ok": ok, "reason": reason, "allowed": verdict["allowed"]})
    return ok, verdict
