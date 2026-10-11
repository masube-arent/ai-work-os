"""Trusted side of the AI execution boundary (Security Fix 002).

Configured through the environment the controller gives the runner:
  AIWOS_AGENT_USER    unprivileged user that runs every AI CLI and every
                      command an AI may have written (acceptance commands)
  AIWOS_AGENT_GROUP   group shared by the trusted user and the agent user,
                      used only for workspace trees
  AIWOS_AGENT_PYTHON  interpreter named in the sudoers rule (/usr/bin/python3)

Unset AIWOS_AGENT_USER means "not isolated": commands run as the caller, as
in v0.2. The controller refuses to publish to a network remote in that case.
"""
import grp
import json
import os
import pwd
import stat
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT_EXEC = HERE / "agent-exec.py"
PROBE = HERE / "isolation-probe.py"
SUDO_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}


def agent_user():
    return os.environ.get("AIWOS_AGENT_USER") or None


def agent_group():
    return os.environ.get("AIWOS_AGENT_GROUP") or None


def agent_python():
    return os.environ.get("AIWOS_AGENT_PYTHON") or "/usr/bin/python3"


def isolated():
    return bool(agent_user())


def agent_env(env):
    """The environment an agent gets: the caller's allowlisted values with
    HOME and USER of the agent user (its own CLI logins live there)."""
    if not isolated():
        return env
    pw = pwd.getpwnam(agent_user())
    out = dict(env)
    out.update(HOME=pw.pw_dir, USER=pw.pw_name, LOGNAME=pw.pw_name)
    out.pop("TMPDIR", None)
    return out


def wrap(argv, env, cwd, timeout):
    """Return (argv, env, cwd) that start `argv` as the agent user. The agent
    environment travels as an argument to agent-exec, so sudo needs no
    environment passing and nothing of the caller's environment is inherited."""
    if not isolated():
        return list(argv), env, cwd
    inner = agent_env(env)
    outer = ["sudo", "-n", "-u", agent_user(), "--", agent_python(), str(AGENT_EXEC),
             "--env-json", json.dumps(inner), "--cwd", str(cwd), "--timeout", str(int(timeout)), "--", *argv]
    return outer, dict(SUDO_ENV), "/"


def run(argv, env, cwd, timeout, **kw):
    """subprocess.run through wrap(), for short commands (version, auth, probe)."""
    outer, outer_env, outer_cwd = wrap(argv, env, cwd, timeout)
    return subprocess.run(outer, env=outer_env, cwd=outer_cwd, timeout=timeout + 15, **kw)


def share(path, writable):
    """Give the agent group access to a workspace directory tree: read-only, or
    read-write with setgid directories so files the agent creates stay in the
    group. Others get nothing. No-op when not isolated."""
    if not isolated():
        return
    gid = grp.getgrnam(agent_group()).gr_gid
    for root, dirs, files in os.walk(path):
        for p in [root] + [os.path.join(root, n) for n in dirs + files]:
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                os.chown(p, -1, gid, follow_symlinks=False)
                continue
            os.chown(p, -1, gid)
            owner = st.st_mode & 0o700
            if stat.S_ISDIR(st.st_mode):
                os.chmod(p, (0o2770 if writable else 0o0750))
            else:
                group = 0o060 if writable else 0o040
                if owner & 0o100:
                    group |= 0o010
                os.chmod(p, owner | group)


def traversable(path):
    """Let the agent group enter (not list or write) a directory on the way to a tree."""
    if not isolated():
        return
    gid = grp.getgrnam(agent_group()).gr_gid
    os.chown(path, -1, gid)
    os.chmod(path, 0o0710)


def read_untrusted(path, limit=2_000_000):
    """Read a file the agent wrote, as the trusted user, without being tricked
    into reading something else: no symlinks (O_NOFOLLOW), regular files only,
    and no hard links (a link count above 1 could point at a trusted file).
    Returns bytes, or None if the file is absent or refused."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            return f.read(limit)
    finally:
        os.close(fd)


def probe(read_files, write_dirs, pids, timeout=60):
    """Ask the agent user to try what it must not be able to do. Returns
    (ok, report). ok is True only if the probe ran as a different user than
    the caller and every attempt was refused."""
    if not isolated():
        return False, {"error": "not isolated: AIWOS_AGENT_USER is not set"}
    targets = {"read_files": [str(p) for p in read_files], "write_dirs": [str(p) for p in write_dirs],
               "pids": list(pids), "env_names": ["GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"]}
    try:
        out = run([agent_python(), str(PROBE), json.dumps(targets)], {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                  "/", timeout, capture_output=True, text=True)
        report = json.loads(out.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return False, {"error": f"probe did not run: {type(exc).__name__}: {exc}"}
    allowed = [k for k, v in report.get("results", {}).items() if v == "ALLOWED"]
    same_user = report.get("uid") == os.getuid()
    ok = not allowed and not same_user and report.get("uid") is not None
    report.update(allowed=allowed, same_user=same_user, ok=ok)
    return ok, report
