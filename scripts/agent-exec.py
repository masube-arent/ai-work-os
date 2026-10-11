#!/usr/bin/env python3
"""Run one untrusted command as the AI execution user (Security Fix 002).

Invoked by the trusted side as
  sudo -n -u <agent_user> -- /usr/bin/python3 <code_dir>/scripts/agent-exec.py \
       --env-json JSON --cwd DIR --timeout SECONDS -- ARGV...
and therefore already running as the unprivileged agent user. It:

- starts ARGV in a new session with exactly the environment in --env-json
  (no variable is inherited, so no token can arrive by accident);
- sets umask 0002 so files the agent creates in its group-shared workspace
  stay readable to the trusted side;
- forwards SIGTERM/SIGINT (which sudo relays from the trusted side) to the
  whole session, and SIGKILLs it after a grace period;
- kills the session if its own parent (sudo) dies or the wall-clock timeout
  passes, so an agent never outlives the stage that started it.

Exit code: the command's, 124 on timeout, 128+N if killed by signal N.
This file must be owned by root and not writable by the agent user; the
sudoers rule names it by absolute path (deploy/isolation/).
"""
import ctypes
import json
import os
import signal
import sys
import time

GRACE_S = 10
PR_SET_PDEATHSIG = 1


def main(argv):
    if "--" not in argv:
        print("usage: agent-exec.py --env-json JSON --cwd DIR --timeout S -- ARGV...", file=sys.stderr)
        return 2
    split = argv.index("--")
    opts, command = argv[:split], argv[split + 1:]
    args = dict(zip(opts[::2], opts[1::2]))
    env = json.loads(args["--env-json"])
    cwd, timeout = args["--cwd"], float(args["--timeout"])

    # die with the parent: if sudo (and so the trusted caller) is gone, stop
    try:
        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except (OSError, AttributeError):
        pass
    parent = os.getppid()
    os.umask(0o002)

    pid = os.fork()
    if pid == 0:
        os.setsid()
        os.chdir(cwd)
        try:
            os.execvpe(command[0], command, env)
        except OSError as exc:
            print(f"agent-exec: cannot run {command[0]}: {exc}", file=sys.stderr)
            os._exit(127)

    stop = {"signal": None}

    def relay(signum, frame):
        stop["signal"] = signum

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, relay)

    deadline = time.time() + timeout
    timed_out = False
    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
        if stop["signal"] or time.time() > deadline or os.getppid() != parent:
            timed_out = not stop["signal"] and time.time() > deadline
            first = stop["signal"] or signal.SIGTERM
            _signal_group(pid, first)
            end = time.time() + GRACE_S
            while time.time() < end:
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    break
                time.sleep(0.1)
            _signal_group(pid, signal.SIGKILL)
            if not done:
                _, status = os.waitpid(pid, 0)
            break
        time.sleep(0.1)
    _signal_group(pid, signal.SIGKILL)  # anything the command left behind in its session
    if timed_out:
        return 124
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return os.WEXITSTATUS(status)


def _signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
