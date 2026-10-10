#!/usr/bin/env bash
# AI WORK OS environment diagnosis.
#
# Usage: bash scripts/doctor.sh
#
# Reports which tools are present and how they can be run. It never prints
# tokens, auth output or environment values: authentication is reported as a
# yes/no from the exit code alone. It never starts an AI session; the only
# CLI invocations are --version and --help.
#
# Exit 0 when every REQUIRED item is present (git, python3 >= 3.9, jsonschema),
# 1 otherwise. OPTIONAL items (gh, claude, codex) are reported but never fail.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
missing_required=0

row() { printf '%-10s %-9s %-14s %s\n' "$1" "$2" "$3" "$4"; }
# Run with a 15 s limit where `timeout` exists (GNU coreutils; absent on stock macOS).
bounded() { if command -v timeout >/dev/null 2>&1; then timeout 15 "$@"; else "$@"; fi; }
# First line of a command's output, bounded in time and length.
probe() { bounded "$@" 2>/dev/null | head -n 1 | cut -c1-60; }
# Does the command's --help text contain a pattern? (fixed string)
help_has() { bounded "$1" --help 2>/dev/null | grep -qF -- "$2"; }

echo "AI WORK OS doctor — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo
row ITEM LEVEL STATUS DETAIL
row ---- ----- ------ ------

# --- required ---------------------------------------------------------------
if command -v git >/dev/null 2>&1; then
  row git required ok "$(probe git --version)"
else
  row git required MISSING "install git"; missing_required=1
fi

if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
  row python3 required ok "$(probe python3 --version)"
else
  row python3 required MISSING "python3 >= 3.9 is required"; missing_required=1
fi

if python3 -c 'import jsonschema' >/dev/null 2>&1; then
  row jsonschema required ok "$(python3 -c 'from importlib.metadata import version; print(version("jsonschema"))' 2>/dev/null)"
else
  row jsonschema required MISSING "pip install -r requirements.txt"; missing_required=1
fi

# --- optional: GitHub CLI ---------------------------------------------------
if command -v gh >/dev/null 2>&1; then
  row gh optional ok "$(probe gh --version)"
  # Output discarded on purpose: gh auth status can print account and token details.
  if bounded gh auth status >/dev/null 2>&1; then
    row gh-auth optional ok "authenticated"
  else
    row gh-auth optional NO "not authenticated or token invalid (run: gh auth login)"
  fi
else
  row gh optional absent "https://cli.github.com/"
  row gh-auth optional skipped "gh not installed"
fi

# --- optional: Claude Code --------------------------------------------------
if command -v claude >/dev/null 2>&1; then
  row claude optional ok "$(probe claude --version)"
  modes="interactive"
  help_has claude "--print" && modes="$modes, non-interactive(-p/--print)"
  help_has claude "--permission-mode" && modes="$modes, --permission-mode"
  help_has claude "--disallowedTools" && modes="$modes, --disallowedTools"
  row claude-run optional info "per --help: $modes"
  # Only the loggedIn flag and method are read; the account is never printed.
  if method="$(bounded claude auth status 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("authMethod") or "?"); sys.exit(0 if d.get("loggedIn") else 1)' 2>/dev/null)"; then
    row claude-auth optional ok "logged in ($method)"
  else
    row claude-auth optional NO "not logged in (run: claude auth login)"
  fi
else
  row claude optional absent "Claude Code CLI not found"
fi

# --- optional: Codex CLI ----------------------------------------------------
if command -v codex >/dev/null 2>&1; then
  row codex optional ok "$(probe codex --version)"
  modes="interactive"
  help_has codex "exec" && modes="$modes, 'exec' subcommand listed"
  help_has codex "--sandbox" && modes="$modes, --sandbox"
  help_has codex "--ask-for-approval" && modes="$modes, --ask-for-approval"
  row codex-run optional info "per --help: $modes"
  if bounded codex login status >/dev/null 2>&1; then
    row codex-auth optional ok "logged in"
  else
    row codex-auth optional NO "not logged in (run: codex login --device-auth)"
  fi
else
  row codex optional absent "Codex CLI not found"
fi

# --- repository safety ------------------------------------------------------
if [ -d "$ROOT/.git" ]; then
  hooks="$(git -C "$ROOT" config core.hooksPath || true)"
  if [ "$hooks" = ".githooks" ]; then
    row hooks optional ok "pre-commit secret scan enabled"
  else
    row hooks optional off "enable: git config core.hooksPath .githooks"
  fi
fi

echo
if [ "$missing_required" -ne 0 ]; then
  echo "doctor: REQUIRED items missing — task validation will not run."
  exit 1
fi
echo "doctor: required items present. Optional items above are informational."
exit 0
