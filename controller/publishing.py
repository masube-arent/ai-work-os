"""When the trusted side may publish (Security Fix 002).

Publishing needs credentials. If an AI process could read them, publishing
unattended would hand them to the AI, so it fails closed:

  - publish=none                      never publishes
  - publish=git to a local path       allowed (no credential leaves the machine)
  - any network remote, or publish=github (which always uses GH_TOKEN):
        only while a recent isolation probe, run as the agent user, proved it
        cannot read the token, /proc of the trusted processes, the configured
        credential files, or write the trusted code, state and repository
"""
import os
import time
from pathlib import Path

from . import gitops

ISOLATION_KEY = "isolation"


def publish_allowed(cfg, store):
    if cfg["publish"] == "none":
        return False, "publish=none"
    if cfg["publish"] == "git" and gitops.remote_is_local(cfg["repo_dir"], cfg["remote"]):
        return True, "local remote"
    verdict, checked_at = store.get_meta(ISOLATION_KEY)
    if not verdict or not verdict.get("ok"):
        why = (verdict or {}).get("reason", "isolation never verified")
        return False, f"publishing disabled (fail closed): {why}"
    if time.time() - checked_at > cfg["isolation_check_ttl_s"]:
        return False, "publishing disabled (fail closed): isolation check is stale"
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
    verdict = {"ok": ok, "reason": reason, "uid": report.get("uid"), "allowed": report.get("allowed", []),
               "results": report.get("results", {})}
    store.set_meta(ISOLATION_KEY, verdict)
    store.event("isolation_checked", detail={"ok": ok, "reason": reason, "allowed": verdict["allowed"]})
    return ok, verdict
