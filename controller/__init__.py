"""AI WORK OS v0.3 controller: up to 10 local workers that take task contracts
from GitHub, run them through scripts/aiwos.py, and publish the results back.

See docs/controller.md. No GitHub Actions involved."""

import os as _os
import sys as _sys

# the trusted helpers in scripts/ (scan_secrets, isolation) are used by the controller too
_SCRIPTS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in _sys.path:
    _sys.path.insert(0, _SCRIPTS)
