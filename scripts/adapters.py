"""How each agent CLI is located, authenticated and invoked.

Every option used here was read from the CLI's own --help on the version noted
in agents/<name>.md. Nothing is inferred: a value the CLI does not report is
returned as None and recorded as null.

Adding an agent means adding one Adapter subclass and one entry in ADAPTERS;
the task contract, the runner and the quality rules do not change.
"""
import json
import os
import shutil
import subprocess

# Environment passed to an agent or a check: an allowlist, so tokens such as
# GH_TOKEN, OPENAI_API_KEY or ANTHROPIC_API_KEY never reach a child process.
# Dropping the API-key variables also keeps the CLIs on their login session
# rather than per-call API billing.
BASE_ENV_KEYS = (
    "PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "SHELL",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
)
NEVER_PASS = ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY")


def scrubbed_env(extra_keys=()):
    env = {k: os.environ[k] for k in BASE_ENV_KEYS + tuple(extra_keys)
           if k in os.environ and k not in NEVER_PASS}
    env.setdefault("LANG", "C.UTF-8")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _first_line(argv, env):
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (out.stdout or out.stderr).strip().splitlines()
    return lines[0][:120] if lines else None


class Adapter:
    agent = None      # contract name, e.g. "claude-code"
    cli = None        # executable name, e.g. "claude"
    bin_env = None    # override variable for the executable path
    env_keys = ()     # extra variables the CLI needs

    def locate(self):
        override = os.environ.get(self.bin_env or "")
        if override:
            return override if os.access(override, os.X_OK) else None
        return shutil.which(self.cli)

    def env(self):
        return scrubbed_env(self.env_keys)

    def version(self, path):
        return _first_line([path, "--version"], self.env())

    def auth(self, path):
        """Return (auth_record, refusal_reason_or_None)."""
        raise NotImplementedError

    def argv(self, path, role, workspace, run_dir, schema_path, model):
        raise NotImplementedError

    def parse(self, stdout, run_dir):
        """Return dict: models_reported, session_id, cost_usd, num_turns,
        is_error, structured (reviewer verdict or None)."""
        raise NotImplementedError


class ClaudeCode(Adapter):
    agent, cli, bin_env = "claude-code", "claude", "AIWOS_CLAUDE_BIN"
    # Verified on 2.1.296: `claude auth status` prints JSON with these keys.
    # Only oauth_token (subscription login) is accepted; any other method is
    # refused until someone checks it is not per-call API billing.
    ALLOWED_AUTH = {"oauth_token"}
    WORKER_TOOLS = "Read,Edit,Write,Glob,Grep,Bash"
    REVIEWER_TOOLS = "Read,Glob,Grep"
    DENY = "Bash(git push:*),Bash(git remote:*),Bash(curl:*),Bash(wget:*),WebFetch,WebSearch"

    def auth(self, path):
        record = {"checked": True, "logged_in": None, "method": None, "provider": None}
        try:
            out = subprocess.run([path, "auth", "status"], capture_output=True, text=True,
                                 timeout=30, env=self.env())
            data = json.loads(out.stdout)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return record, "could not read `claude auth status`"
        # Keep three fields only: the status also names config paths and may
        # name the account, which a run record has no need for.
        record.update(logged_in=bool(data.get("loggedIn")), method=data.get("authMethod"),
                      provider=data.get("apiProvider"))
        if not record["logged_in"]:
            return record, "claude is not logged in (run: claude auth login)"
        if record["method"] not in self.ALLOWED_AUTH:
            return record, f"auth method '{record['method']}' is not on the verified allowlist {sorted(self.ALLOWED_AUTH)}"
        return record, None

    def argv(self, path, role, workspace, run_dir, schema_path, model):
        argv = [path, "-p", "--output-format", "json", "--no-session-persistence", "--strict-mcp-config",
                "--disallowedTools", self.DENY]
        if role == "worker":
            argv += ["--permission-mode", "acceptEdits", "--tools", self.WORKER_TOOLS,
                     "--allowedTools", "Bash(python3 -m unittest:*)"]
        else:
            with open(schema_path, encoding="utf-8") as f:
                argv += ["--tools", self.REVIEWER_TOOLS, "--json-schema", f.read()]
        if model:
            argv += ["--model", model]
        return argv  # prompt goes to stdin

    def parse(self, stdout, run_dir):
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return {"models_reported": None, "session_id": None, "cost_usd": None,
                    "num_turns": None, "is_error": True, "structured": None}
        usage = data.get("modelUsage")
        models = None
        if isinstance(usage, dict):
            models = [{"model": m, "input_tokens": u.get("inputTokens"), "output_tokens": u.get("outputTokens")}
                      for m, u in usage.items()]
        return {"models_reported": models, "session_id": data.get("session_id"),
                "cost_usd": data.get("total_cost_usd"), "num_turns": data.get("num_turns"),
                "is_error": bool(data.get("is_error")), "structured": data.get("structured_output")}


class CodexCli(Adapter):
    agent, cli, bin_env = "codex-cli", "codex", "AIWOS_CODEX_BIN"
    env_keys = ("CODEX_HOME",)

    def auth(self, path):
        # Verified on 0.162.1: `codex login status` printed "Not logged in" and
        # exited 1. The wording of a logged-in status has NOT been observed, so
        # the method is only recognised from the words below; anything else is
        # refused rather than guessed.
        record = {"checked": True, "logged_in": None, "method": None, "provider": None}
        try:
            out = subprocess.run([path, "login", "status"], capture_output=True, text=True,
                                 timeout=30, env=self.env())
        except (OSError, subprocess.TimeoutExpired):
            return record, "could not run `codex login status`"
        text = (out.stdout + out.stderr).lower()
        record["logged_in"] = out.returncode == 0
        if not record["logged_in"]:
            return record, "codex is not logged in (run: codex login --device-auth)"
        if "api key" in text:
            record["method"] = "api_key"
            return record, "codex is logged in with an API key; per-call API billing is not allowed"
        if "chatgpt" in text:
            record["method"] = "chatgpt"
            return record, None
        return record, "codex login method not recognised; refusing until it is verified"

    def argv(self, path, role, workspace, run_dir, schema_path, model):
        sandbox = "workspace-write" if role == "worker" else "read-only"
        argv = [path, "exec", "-s", sandbox, "-C", str(workspace), "--json", "--ephemeral",
                "-o", str(run_dir / "last-message.txt")]
        if role == "reviewer":
            argv += ["--output-schema", str(schema_path)]
        if model:
            argv += ["-m", model]
        return argv + ["-"]  # `-`: read the prompt from stdin

    def parse(self, stdout, run_dir):
        # Event names in `--json` output have not been observed on this
        # version; ids and usage are picked up only if present.
        session_id, models, turns = None, None, 0
        usage_in = usage_out = None
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            session_id = session_id or ev.get("thread_id") or ev.get("session_id")
            usage = ev.get("usage")
            if isinstance(usage, dict):
                turns += 1
                usage_in = (usage_in or 0) + (usage.get("input_tokens") or 0)
                usage_out = (usage_out or 0) + (usage.get("output_tokens") or 0)
            if isinstance(ev.get("model"), str):
                models = [{"model": ev["model"], "input_tokens": None, "output_tokens": None}]
        if models and usage_in is not None:
            models[0].update(input_tokens=usage_in, output_tokens=usage_out)
        structured = None
        last = run_dir / "last-message.txt"
        if last.is_file():
            try:
                structured = json.loads(last.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                structured = None
        return {"models_reported": models, "session_id": session_id, "cost_usd": None,
                "num_turns": turns or None, "is_error": False, "structured": structured}


ADAPTERS = {a.agent: a for a in (ClaudeCode(), CodexCli())}
