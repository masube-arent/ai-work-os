"""WSL2 holes the OS-user split does not close (local CLI on Windows).

Under WSL2 two Windows features reach past Linux users entirely:

- interop: any Linux user, the agent included, can start a Windows program
  (cmd.exe, powershell.exe), which runs as the logged-in Windows user;
- automount (drvfs): the Windows drives appear under /mnt/<letter>, and their
  Linux permissions are synthesised, so the agent can usually read the Windows
  user's files (other AI logins, git credentials, SSH keys).

Either one voids the isolation the controller relies on, so under WSL both
must be off before any AI runs (the fix: /etc/wsl.conf, docs/local-cli.md).
Nothing here needs WSL to run: the paths are parameters, for tests.
"""
import re
from pathlib import Path

BINFMT_DIR = "/proc/sys/fs/binfmt_misc"
PROC_VERSION = "/proc/version"
PROC_MOUNTS = "/proc/mounts"
DRIVE_SOURCE = re.compile(r"^[A-Za-z]:")


def is_wsl(binfmt_dir=BINFMT_DIR, proc_version=PROC_VERSION):
    if any(Path(binfmt_dir).glob("WSLInterop*")):
        return True
    try:
        return "microsoft" in Path(proc_version).read_text().lower()
    except OSError:
        return False


def problems(binfmt_dir=BINFMT_DIR, mounts=PROC_MOUNTS):
    """What under this WSL would let the agent reach Windows. Empty = none found."""
    found = []
    for entry in sorted(Path(binfmt_dir).glob("WSLInterop*")):
        try:
            state = entry.read_text().splitlines()[0].strip()
        except (OSError, IndexError):
            state = "unreadable"
        if state != "disabled":
            found.append(f"Windows interop is {state} ({entry.name}): any Linux user can start Windows "
                         f"programs as the Windows user; set [interop] enabled=false in /etc/wsl.conf")
    try:
        lines = Path(mounts).read_text().splitlines()
    except OSError:
        return found + [f"cannot read {mounts}: Windows drive mounts unknown"]
    for line in lines:
        fields = line.split()
        if len(fields) < 4:
            continue
        source, target, fstype, options = fields[:4]
        if fstype == "drvfs" or "aname=drvfs" in options or DRIVE_SOURCE.match(source):
            found.append(f"Windows drive mounted at {target} ({fstype}): the agent may read Windows files; "
                         f"set [automount] enabled=false in /etc/wsl.conf")
    return found
