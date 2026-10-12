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


def kill_tree(pid, grace=10.0, first=signal.SIGTERM):
    """Signal pid and every descendant, wait up to `grace`, then SIGKILL."""
    tree = [pid] + descendants(pid)
    for p in reversed(tree):
        try:
            os.kill(p, first)
        except ProcessLookupError:
            pass
    deadline = time.time() + grace
    while time.time() < deadline and any(pid_alive(p) for p in tree):
        time.sleep(0.2)
    for p in tree + descendants(pid):
        if pid_alive(p):
            try:
                os.kill(p, signal.SIGKILL)
            except ProcessLookupError:
                pass
