#!/usr/bin/env bash
# Create the OS-level separation of Security Fix 002. Run as root.
#
#   deploy/isolation/setup.sh [CODE_DIR] [CTL_USER] [AGENT_USER] [GROUP]
#
# defaults: /opt/aiwos/code aiwos-ctl aiwos-agent aiwos-ws
#
#   CTL_USER    runs the controller, the workers and the GitHub publisher;
#               holds GH_TOKEN and any git credentials
#   AGENT_USER  runs every AI CLI and every command an AI may have written;
#               holds only its own Claude/Codex logins
#   GROUP       shared by both, used only for workspace trees
#
# CODE_DIR must be owned by root and not writable by either user.
set -euo pipefail
CODE_DIR="${1:-/opt/aiwos/code}"
CTL="${2:-aiwos-ctl}"
AGENT="${3:-aiwos-agent}"
GROUP="${4:-aiwos-ws}"
PY=/usr/bin/python3

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
[ -x "$PY" ] || { echo "$PY not found" >&2; exit 1; }
[ -f "$CODE_DIR/scripts/agent-exec.py" ] || { echo "$CODE_DIR/scripts/agent-exec.py not found" >&2; exit 1; }

getent group "$GROUP" >/dev/null || groupadd --system "$GROUP"
for u in "$CTL" "$AGENT"; do
  id "$u" >/dev/null 2>&1 || useradd --system --create-home --home-dir "/var/lib/$u" --shell /usr/sbin/nologin "$u"
  usermod -a -G "$GROUP" "$u"
  chmod 0700 "$(getent passwd "$u" | cut -d: -f6)"
done

# the code the agent user runs must not be writable by it (nor by the controller)
if find "$CODE_DIR" \( -user "$CTL" -o -user "$AGENT" -o -perm -0002 \) -print -quit | grep -q .; then
  echo "WARNING: $CODE_DIR has files owned by $CTL/$AGENT or world-writable; fix before use" >&2
fi

rule=$(mktemp)
cat > "$rule" <<RULE
Defaults:$CTL !requiretty
Defaults:$CTL env_reset
$CTL ALL=($AGENT) NOPASSWD: $PY $CODE_DIR/scripts/agent-exec.py *
RULE
visudo -cf "$rule" >/dev/null
install -m 0440 -o root -g root "$rule" /etc/sudoers.d/aiwos
rm -f "$rule"

# kernel settings the isolation check requires
sysctl -q fs.protected_hardlinks=1 fs.protected_symlinks=1 || true

echo "ok: $CTL may run only $CODE_DIR/scripts/agent-exec.py as $AGENT"
echo "next: log the AI CLIs in as $AGENT (sudo -u $AGENT -H claude auth login; codex login --device-auth),"
echo "      set agent_user=$AGENT agent_group=$GROUP in the controller config"
