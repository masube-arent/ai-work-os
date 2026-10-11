#!/usr/bin/env python3
"""Isolation probe: run as the agent user, try everything an AI must not be
able to do, and report what happened. Never prints file contents.

  isolation-probe.py '{"read_files": [...], "write_dirs": [...], "pids": [...], "env_names": [...]}'

Each attempt is "blocked" (refused by the OS), "missing" (target absent) or
"ALLOWED". The trusted side treats any ALLOWED, or a probe that did not run,
as a failed isolation check.
"""
import json
import os
import sys


def try_read(path):
    try:
        with open(path, "rb") as f:
            f.read(1)
        return "ALLOWED"
    except FileNotFoundError:
        return "missing"
    except (PermissionError, IsADirectoryError, OSError):
        return "blocked"


def try_write(directory):
    target = os.path.join(directory, f".aiwos-probe-{os.getpid()}")
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "blocked"
    os.close(fd)
    os.unlink(target)
    return "ALLOWED"


def main(argv):
    targets = json.loads(argv[0])
    results = {}
    for p in targets.get("read_files", []):
        results[f"read:{p}"] = try_read(p)
    for d in targets.get("write_dirs", []):
        results[f"write:{d}"] = try_write(d)
    for pid in targets.get("pids", []):
        results[f"environ:{pid}"] = try_read(f"/proc/{pid}/environ")
        results[f"signal:{pid}"] = _try_signal(pid)
    for name in targets.get("env_names", []):
        results[f"env:{name}"] = "ALLOWED" if name in os.environ else "blocked"
    print(json.dumps({"uid": os.getuid(), "gid": os.getgid(), "results": results}))
    return 0


def _try_signal(pid):
    try:
        os.kill(pid, 0)  # permission check only; signal 0 delivers nothing
        return "ALLOWED"
    except ProcessLookupError:
        return "missing"
    except PermissionError:
        return "blocked"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
