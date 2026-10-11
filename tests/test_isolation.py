"""Security Fix 002: tests for the OS-level split between the trusted
controller user and the AI execution user.

Needs root (to build sandboxes owned by other users) and the users, group and
sudo rule from deploy/isolation/setup.sh (aiwos-ctl, aiwos-agent, aiwos-ws).
Without them every test here is skipped with the reason, and the isolation is
UNVERIFIED on that machine.

The adversarial checks reuse scripts/isolation-probe.py (the trusted probe,
which only reports blocked/missing/ALLOWED and never prints file contents),
run as the agent user. The sentinel token is random per run and never a real
credential; the remote has a network-looking URL mapped to a local bare repo.
"""
import json
import os
import pwd
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from controller_harness import ROOT, contract  # noqa: E402
sys.path.insert(0, str(ROOT / "scripts"))

CTL, AGENT, GROUP = "aiwos-ctl", "aiwos-agent", "aiwos-ws"
PY = "/usr/bin/python3"


def environment_problem():
    if os.geteuid() != 0:
        return "needs root to build sandboxes owned by another user"
    for u in (CTL, AGENT):
        try:
            pwd.getpwnam(u)
        except KeyError:
            return f"user {u} missing: run deploy/isolation/setup.sh {ROOT}"
    probe = as_ctl(["sudo", "-n", "-u", AGENT, "--", PY, str(ROOT / "scripts" / "agent-exec.py"),
                    "--env-json", '{"PATH": "/usr/bin:/bin"}', "--cwd", "/", "--timeout", "10", "--",
                    "/usr/bin/id", "-un"])
    if probe.stdout.strip() != AGENT:
        return f"sudo rule {CTL} -> {AGENT} not working: {probe.stderr.strip()[:200]}"
    return None


def as_ctl(argv, **kw):
    """Run argv as the aiwos-ctl user (reset groups), capturing output."""
    return subprocess.run(["setpriv", f"--reuid={CTL}", f"--regid={CTL}", "--init-groups", "--", *argv],
                          capture_output=True, text=True, **kw)


PROBLEM = environment_problem()


@unittest.skipIf(PROBLEM, PROBLEM or "")
class IsolationBase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="aiwos-iso-"))
        os.chmod(self.tmp, 0o755)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.token = "ghp_" + secrets.token_hex(20)  # sentinel; never a real credential

    def chown(self, path, user):
        pw = pwd.getpwnam(user)
        subprocess.run(["chown", f"{pw.pw_uid}:{pw.pw_gid}", "-R", str(path)], check=True)


class PenetrationProbe(IsolationBase):
    """Run the probe as the agent user against everything it must not reach."""

    def run_probe(self, targets):
        probe = ROOT / "scripts" / "isolation-probe.py"
        r = as_ctl(["sudo", "-n", "-u", AGENT, "--", PY, str(ROOT / "scripts" / "agent-exec.py"),
                    "--env-json", json.dumps({"PATH": "/usr/bin:/bin"}),
                    "--cwd", "/", "--timeout", "30", "--", PY, str(probe), json.dumps(targets)])
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_agent_cannot_reach_credentials_code_or_controller_memory(self):
        # a token env file, a controller-owned secret, the trusted code and state
        secret_file = self.tmp / "controller.env"
        secret_file.write_text(f"GH_TOKEN={self.token}\n")
        os.chmod(secret_file, 0o600)  # owned by root (the test), mode 0600
        state = self.tmp / "state"
        state.mkdir()
        (state / "aiwos.db").write_text("trusted state\n")
        self.chown(state, CTL)
        os.chmod(state, 0o700)
        # a live controller-side process holding the token in its environment
        holder = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"], env={**os.environ, "GH_TOKEN": self.token})
        self.addCleanup(lambda: (holder.kill(), holder.wait()))

        report = self.run_probe({
            "read_files": [str(secret_file), str(state / "aiwos.db")],
            "write_dirs": [str(ROOT / "scripts"), str(ROOT / "controller"), str(ROOT), str(state)],
            "pids": [holder.pid, os.getpid()],
            "env_names": ["GH_TOKEN"],
        })
        self.assertNotEqual(report["uid"], os.getuid())  # really a different user
        allowed = [k for k, v in report["results"].items() if v == "ALLOWED"]
        self.assertEqual(allowed, [], f"agent could do: {allowed}")
        # specifically: the token is not in its own env, and the holder's /proc/environ is refused
        self.assertEqual(report["results"]["env:GH_TOKEN"], "blocked")
        self.assertEqual(report["results"][f"environ:{holder.pid}"], "blocked")

    def test_agent_can_use_only_its_shared_workspace(self):
        import isolation
        os.environ.update(AIWOS_AGENT_USER=AGENT, AIWOS_AGENT_GROUP=GROUP, AIWOS_AGENT_PYTHON=PY)
        self.addCleanup(lambda: [os.environ.pop(k) for k in
                                 ("AIWOS_AGENT_USER", "AIWOS_AGENT_GROUP", "AIWOS_AGENT_PYTHON")])
        tree = self.tmp / "ws" / "tree"
        tree.mkdir(parents=True)
        (tree / "file.txt").write_text("work here\n")
        isolation.traversable(self.tmp / "ws")
        isolation.share(tree, writable=True)
        report = self.run_probe({"write_dirs": [str(tree)], "read_files": [str(tree / "file.txt")],
                                 "pids": [], "env_names": []})
        self.assertEqual(report["results"][f"write:{tree}"], "ALLOWED")  # its own workspace: yes
        self.assertEqual(report["results"][f"read:{tree / 'file.txt'}"], "ALLOWED")


class ProbeVerdict(IsolationBase):
    def test_probe_reports_not_ok_when_the_agent_can_read_a_target(self):
        import isolation
        os.environ.update(AIWOS_AGENT_USER=AGENT, AIWOS_AGENT_GROUP=GROUP, AIWOS_AGENT_PYTHON=PY)
        self.addCleanup(lambda: [os.environ.pop(k) for k in
                                 ("AIWOS_AGENT_USER", "AIWOS_AGENT_GROUP", "AIWOS_AGENT_PYTHON")])
        world = self.tmp / "world-readable"
        world.write_text("anyone\n")
        os.chmod(world, 0o644)
        ok, report = isolation.probe([world], [], [])
        self.assertFalse(ok)  # a reachable target means isolation is NOT proven
        self.assertIn(f"read:{world}", report["allowed"])
        # and a target the agent cannot reach alone makes ok True
        locked = self.tmp / "locked"
        locked.write_text("secret\n")
        os.chmod(locked, 0o600)  # owned by root (the test), unreadable to the agent
        ok2, report2 = isolation.probe([locked], [], [])
        self.assertTrue(ok2, report2)


class SafeReadOfAgentOutput(IsolationBase):
    def test_trusted_read_refuses_symlink_and_hardlink(self):
        import isolation
        secret = self.tmp / "secret"
        secret.write_text(self.token + "\n")
        link = self.tmp / "via-symlink"
        link.symlink_to(secret)
        self.assertIsNone(isolation.read_untrusted(link))  # O_NOFOLLOW
        hard = self.tmp / "via-hardlink"
        os.link(secret, hard)
        self.assertIsNone(isolation.read_untrusted(hard))  # st_nlink > 1
        plain = self.tmp / "plain"
        plain.write_text("ok\n")
        self.assertEqual(isolation.read_untrusted(plain), b"ok\n")


class FullCycleIsolated(IsolationBase):
    """A worker/check/review cycle with the agent user actually running, through
    the controller, publishing to a network-looking (but local) remote."""

    def build(self):
        from controller_harness import git
        # network-looking remote mapped to a local bare repo
        bare = self.tmp / "remote.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(bare))
        seed = self.tmp / "seed"
        shutil.copytree(ROOT, seed, ignore=shutil.ignore_patterns(".git", "runs", "__pycache__", ".test-evidence"))
        (seed / "tasks" / "ISO-0001").mkdir(parents=True)
        c = contract("ISO-0001")
        c["objective"] = "Isolation test task: create tasks/ISO-0001/out.txt containing 'done'."
        c["allowed_paths"] = ["tasks/ISO-0001"]
        (seed / "tasks" / "ISO-0001" / "task.json").write_text(json.dumps(c, ensure_ascii=False, indent=2))
        git(seed, "init", "-q", "-b", "main")
        git(seed, "add", "-A")
        git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
        git(seed, "push", "-q", str(bare), "main")
        repo = self.tmp / "repo"
        git(self.tmp, "clone", "-q", str(bare), str(repo))
        git(repo, "config", "user.name", "aiwos-controller")
        git(repo, "config", "user.email", "ctl@localhost")
        # make the origin URL look like a network remote, served by the local bare repo
        git(repo, "remote", "set-url", "origin", "https://github.invalid/aiwos/iso.git")
        git(repo, "config", f"url.{bare}.insteadOf", "https://github.invalid/aiwos/iso.git")
        bins = self.tmp / "bin"
        bins.mkdir()
        spy = self.tmp / "spy.jsonl"
        stub = ('#!/usr/bin/env python3\n'
                'import json,os,sys,time\n'
                'HERE=os.path.dirname(os.path.abspath(__file__)); CLI=os.path.basename(__file__).split("-")[-1]\n'
                'a=sys.argv[1:]\n'
                'if a==["--version"]:\n print("0 (stub "+CLI+")"); sys.exit(0)\n'
                'if CLI=="claude" and a[:2]==["auth","status"]:\n'
                ' print(json.dumps({"loggedIn":True,"authMethod":"oauth_token","apiProvider":"firstParty"})); sys.exit(0)\n'
                'if CLI=="codex" and a[:2]==["login","status"]:\n print("Logged in using ChatGPT"); sys.exit(0)\n'
                'p=sys.stdin.read(); rev=("--json-schema" in a) or ("read-only" in a)\n'
                'open(%r,"a").write(json.dumps({"cli":CLI,"role":"reviewer" if rev else "worker","uid":os.getuid()})+"\\n")\n'
                'import re\n'
                'if rev:\n'
                ' ids=re.findall(r"\\*\\*(AC\\d+)\\*\\*",p); rv={"criteria":[{"criterion_id":i,"verdict":"PASS","reason":"s"} for i in ids],"defects":[],"summary":"s"}\n'
                ' if CLI=="codex":\n  open(a[a.index("-o")+1],"w").write(json.dumps(rv)); print(json.dumps({"type":"thread.started","thread_id":"t"}))\n'
                ' else:\n  print(json.dumps({"type":"result","is_error":False,"session_id":"r","num_turns":1,"result":"","structured_output":rv,"modelUsage":{"m":{"inputTokens":1,"outputTokens":1}}}))\n'
                ' sys.exit(0)\n'
                'd=re.search(r"変更してよいパス: (\\S+)",p).group(1).rstrip(","); os.makedirs(d,exist_ok=True); open(os.path.join(d,"out.txt"),"w").write("done\\n")\n'
                'print(json.dumps({"type":"result","is_error":False,"session_id":"w","num_turns":1,"result":"done","modelUsage":{"m":{"inputTokens":1,"outputTokens":1}}}) if CLI=="claude" else json.dumps({"type":"thread.started","thread_id":"t"}))\n'
                % str(spy))
        for cli in ("claude", "codex"):
            f = bins / f"stub-{cli}"
            f.write_text(stub)
            f.chmod(0o755)
        (bins / "stub.json").write_text("{}")
        spy.write_text("")  # agent user appends here; it cannot create files in the root-owned tmp
        os.chmod(spy, 0o666)
        cfg = {
            "repo_dir": str(repo), "state_dir": str(self.tmp / "state"),
            "workspaces_dir": str(self.tmp / "workspaces"), "code_dir": str(ROOT),
            "task_globs": ["tasks/ISO-0001/task.json"], "workers": 3, "model_concurrency": 2,
            "publish": "git", "poll_interval_s": 0.5, "sync_interval_s": 5, "lease_ttl_s": 30, "heartbeat_s": 2,
            "kill_grace_s": 5, "isolation_check_ttl_s": 600,
            "stage_timeout_s": {"work": 120, "check": 60, "review": 120, "judge": 60, "resume": 30},
            "agent_user": AGENT, "agent_group": GROUP, "agent_python": PY,
            "credential_files": [], "agent_bins": {"claude": str(bins / "stub-claude"), "codex": str(bins / "stub-codex")},
        }
        self.cfg = self.tmp / "controller.json"
        self.cfg.write_text(json.dumps(cfg))
        (self.tmp / "state").mkdir()
        (self.tmp / "workspaces").mkdir()
        self.bare, self.repo, self.spy = bare, repo, spy
        # everything the controller writes to is owned by the controller user;
        # the code stays root-owned and not writable by either user
        for d in ("state", "workspaces", "repo", "seed", "bin", "remote.git"):
            self.chown(self.tmp / d, CTL)
        self.chown(self.cfg, CTL)

    def ctl_env(self):
        home = pwd.getpwnam(CTL).pw_dir
        return {**os.environ, "GH_TOKEN": self.token, "HOME": home}

    def controller(self, *args):
        return as_ctl([PY, "-m", "controller", *args, "--config", str(self.cfg)], cwd=str(ROOT), env=self.ctl_env())

    def db(self):
        # read-only, and never create the file: otherwise this root process would
        # make a root-owned aiwos.db that the controller user cannot write
        path = self.tmp / "state" / "aiwos.db"
        if not path.exists():
            raise sqlite3.OperationalError("db not created yet")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def test_cycle_runs_agents_as_agent_user_and_publishes(self):
        self.build()
        p = subprocess.Popen(["setpriv", f"--reuid={CTL}", f"--regid={CTL}", "--init-groups", "--",
                              PY, "-m", "controller", "run", "--config", str(self.cfg)], cwd=str(ROOT),
                             env=self.ctl_env(),
                             stdout=open(self.tmp / "controller.log", "wb"), stderr=subprocess.STDOUT)
        try:
            end = time.time() + 180
            ok = False
            while time.time() < end:
                try:
                    with self.db() as con:
                        rows = [dict(r) for r in con.execute("SELECT task_id, stage, state FROM tasks")]
                    if rows and all(r["state"] == "held" for r in rows):
                        ok = True
                        break
                except sqlite3.OperationalError:
                    pass
                time.sleep(1)
            if not ok:
                self.fail("task did not settle; controller log tail:\n" +
                          "\n".join((self.tmp / "controller.log").read_text(errors="replace").splitlines()[-30:]))
        finally:
            p.terminate()
            try:
                p.wait(timeout=60)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        # the stub agents ran as the agent user, not the controller user
        calls = [json.loads(l) for l in self.spy.read_text().splitlines() if l.strip()]
        self.assertTrue(calls)
        agent_uid = pwd.getpwnam(AGENT).pw_uid
        self.assertTrue(all(c["uid"] == agent_uid for c in calls), calls)
        # it was published to the (local) network-looking remote
        from controller_harness import git
        branches = git(self.tmp, "--git-dir", str(self.bare), "for-each-ref", "--format=%(refname)",
                       "refs/heads/aiwos/task/ISO-0001")
        self.assertIn("aiwos/task/ISO-0001", branches)
        # isolation was recorded ok, so publishing was allowed
        with self.db() as con:
            meta = {r["key"]: json.loads(r["value"]) for r in con.execute("SELECT key, value FROM meta")}
        self.assertTrue(meta["isolation"]["ok"], meta["isolation"])


if __name__ == "__main__":
    unittest.main()
