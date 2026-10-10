#!/usr/bin/env bash
# Local quality gate. Run before every commit.
#   bash scripts/check.sh
# Steps: validate every task contract, run the unit tests, scan for secrets,
# run the environment diagnosis. Exit non-zero if any of the first three fail.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail=0

step() { echo; echo "=== $1"; }

step "1/4 validate task contracts"
# shellcheck disable=SC2046  # repository paths contain no spaces
python3 scripts/validate-task.py $(find contracts examples -name '*.json' ! -name '*.schema.json' | sort) || fail=1

step "2/4 unit tests"
python3 -m unittest discover -s tests -v 2>&1 || fail=1

step "3/4 secret scan"
python3 scripts/scan_secrets.py || fail=1

step "4/4 environment (informational)"
bash scripts/doctor.sh || echo "(doctor reported missing required items)"

echo
if [ "$fail" -ne 0 ]; then echo "check: FAILED"; exit 1; fi
echo "check: PASSED"
