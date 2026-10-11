"""Process-tree helpers (Linux /proc). The runner starts each agent in its own
session, so killing one process group is not enough: the whole tree is
collected first and then signalled, children before they can be re-parented."""
import os
import signal
import time

from .store import pid_alive


def _children_map():
    children = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(entry))
    return children


def descendants(pid):
    children, out, todo = _children_map(), [], [pid]
    while todo:
        p = todo.pop()
        for c in children.get(p, []):
            out.append(c)
            todo.append(c)
    return out


def cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def find_tagged(tag):
    """Processes of this user whose environment carries AIWOS_LEASE_TAG=tag.
    Finds a stage process even if the worker died before recording its pid."""
    needle = f"AIWOS_LEASE_TAG={tag}".encode()
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/environ", "rb") as f:
                if needle in f.read().split(b"\0"):
                    found.append(int(entry))
        except OSError:
            continue  # gone, or another user's process (the agent's): reached through its parent
    return found


def kill_tree(pid, grace=10.0, first=signal.SIGTERM):
    """Signal pid and every descendant, wait up to `grace`, then SIGKILL."""
    tree = [pid] + descendants(pid)
    for p in reversed(tree):
        try:
            os.kill(p, first)
        except (ProcessLookupError, PermissionError):
            pass  # another user's process (the agent side) is stopped by its own launcher
    deadline = time.time() + grace
    while time.time() < deadline and any(pid_alive(p) for p in tree):
        time.sleep(0.2)
    for p in tree + descendants(pid):
        if pid_alive(p):
            try:
                os.kill(p, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
