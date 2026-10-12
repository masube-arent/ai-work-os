"""Local CLI (scripts/aiwos-local.py, deploy/windows/aiwos.ps1) and the
controller changes it relies on (require_isolation, controller/wsl.py).

The unit tests need nothing special. LocalCliIsolated drives the real CLI as
the controller user against a local bare remote, so it needs root and the
users from deploy/isolation/setup.sh; without them it is skipped and those
behaviours are UNVERIFIED on that machine. No test calls a model: the agent
CLIs are not logged in, and no task is queued.
"""
import importlib.util
import io
import json
import os
import pwd
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from controller_harness import ROOT, git  # noqa: E402

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from controller import publishing, wsl  # noqa: E402
from controller.store import Store  # noqa: E402

spec = importlib.util.spec_from_file_location("aiwos_local", ROOT / "scripts" / "aiwos-local.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

PS1 = ROOT / "deploy" / "windows" / "aiwos.ps1"
CTL, AGENT, GROUP = "aiwos-ctl", "aiwos-agent", "aiwos-ws"


def quiet(fn, *a, **kw):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(*a, **kw)
    return code, out.getvalue(), err.getvalue()


class Tmp(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="aiwos-lcli-"))
        self.addCleanup(lambda: shutil.rmtree(self.d, ignore_errors=True))

    def cfg(self, **kw):
        cfg = {"state_dir": str(self.d / "state"), "workspaces_dir": str(self.d / "ws"),
               "repo_dir": str(self.d / "repo"), "code_dir": str(ROOT), "_config_path": str(self.d / "local.json"),
               "publish": "none", "require_isolation": True, "agent_user": "nobody-agent",
               "agent_group": "nobody-group", "task_globs": ["tasks/*.json", "tasks/*/*.json"], "kill_grace_s": 1}
        cfg.update(kw)
        return cfg


class ConfigAndPidFiles(Tmp):
    def test_pid_log_and_lock_files_never_share_the_config_path(self):
        # the ZIP kept its pid file at <state>/controller-local.json, the same path its
        # PowerShell wrapper used for the config: start overwrote the config
        cfg = self.cfg()
        names = {p.name for p in cli.paths(cfg)}
        self.assertEqual(len(names), 3)
        self.assertTrue(all(not n.endswith(".json") for n in names), names)
        for key in ("state_dir", "workspaces_dir", "repo_dir", "code_dir"):
            self.assertIsNotNone(cli.config_location_problem(Path(cfg[key]) / "x.json", cfg), key)
        self.assertIsNone(cli.config_location_problem(self.d / "home" / "local.json", cfg))

    def test_powershell_default_config_is_outside_the_state_dir(self):
        text = PS1.read_text()
        config = re.search(r"\$LinuxConfig = '([^']+)'", text).group(1)
        self.assertFalse(config.startswith("/var/lib/aiwos/state"), config)

    def test_init_writes_the_local_safe_defaults_and_never_overwrites(self):
        repo = self.d / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        cfgpath = self.d / "home" / "local.json"
        ns = cli.argparse.Namespace  # the arguments main() would parse, without its root refusal
        parser_args = dict(config=str(cfgpath), repo_dir=str(repo), state_dir=str(self.d / "state"),
                           workspaces_dir=str(self.d / "ws"), agent_user="nobody-agent", agent_group="g",
                           credential_file=[], github_repo="o/r")
        code, out, err = quiet(cli.cmd_init, ns(**parser_args))
        self.assertEqual(code, 0, err)
        data = json.loads(cfgpath.read_text())
        self.assertEqual((data["publish"], data["require_isolation"], data["workers"], data["model_concurrency"]),
                         ("none", True, 10, 1))
        self.assertEqual(data["task_globs"], ["tasks/*.json", "tasks/*/*.json"])  # never examples/
        self.assertEqual(cfgpath.stat().st_mode & 0o777, 0o600)
        before = cfgpath.read_bytes()
        code, _, err = quiet(cli.cmd_init, ns(**parser_args))
        self.assertEqual(code, 2)
        self.assertIn("not overwriting", err)
        self.assertEqual(cfgpath.read_bytes(), before)

    def test_init_refuses_unsafe_locations(self):
        repo = self.d / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        base = dict(repo_dir=str(repo), state_dir=str(self.d / "state"), workspaces_dir=str(self.d / "ws"),
                    agent_user="nobody-agent", agent_group="g", credential_file=[], github_repo="o/r")
        cases = {
            "config in state": {**base, "config": str(self.d / "state" / "local.json")},
            "config in workspaces": {**base, "config": str(self.d / "ws" / "local.json")},
            "config in repo": {**base, "config": str(repo / "local.json")},
            "repo is the code": {**base, "config": str(self.d / "c.json"), "repo_dir": str(ROOT)},
            "workspaces in state": {**base, "config": str(self.d / "c2.json"),
                                    "workspaces_dir": str(self.d / "state" / "ws")},
        }
        for name, a in cases.items():
            code, _, _ = quiet(cli.cmd_init, cli.argparse.Namespace(**a))
            self.assertEqual(code, 2, name)
            self.assertFalse(Path(a["config"]).exists(), name)

    def test_local_policy_insists_on_publish_none_and_isolation(self):
        self.assertEqual(cli.local_policy(self.cfg()), [])
        self.assertTrue(cli.local_policy(self.cfg(publish="git")))
        self.assertTrue(cli.local_policy(self.cfg(publish="github")))
        self.assertTrue(cli.local_policy(self.cfg(require_isolation=False)))
        self.assertTrue(cli.local_policy(self.cfg(agent_user=None, agent_group=None)))
        self.assertTrue(cli.local_policy(self.cfg(agent_user=cli.current_user())))
        self.assertTrue(cli.local_policy(self.cfg(code_dir=str(self.d))))  # scripts from another checkout

    def test_boundary_check_refuses_before_any_probe(self):
        with mock.patch("controller.main.refresh_isolation", side_effect=AssertionError("probe must not run")):
            for bad in (self.cfg(publish="git"), self.cfg(agent_user=None, agent_group=None)):
                with self.assertRaises(cli.Refused):
                    cli.boundary_check(bad)


class ProcessSafety(Tmp):
    def write_pid(self, cfg, pid, ticks):
        Path(cfg["state_dir"]).mkdir(parents=True, exist_ok=True)
        cli.paths(cfg)[0].write_text(json.dumps({"pid": pid, "config": cfg["_config_path"], "start_ticks": ticks}))

    def test_stale_or_foreign_pid_is_never_ours(self):
        cfg = self.cfg()
        self.write_pid(cfg, os.getpid(), "bogus")
        self.assertIsNone(cli.controller_pid(cfg))  # wrong start time
        self.write_pid(cfg, os.getpid(), cli.process_start_ticks(os.getpid()))
        self.assertIsNone(cli.controller_pid(cfg))  # live, right start time, but not a controller
        self.write_pid(cfg, 2 ** 22 + 7, "1")
        self.assertIsNone(cli.controller_pid(cfg))  # no such process

    def test_a_matching_command_line_alone_is_not_enough(self):
        # PID reuse: a process that looks like this config's controller but started
        # at another time than the one recorded is not the controller we started
        cfg = self.cfg()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-m", "controller", "run",
                                 "--config", cfg["_config_path"]])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        for _ in range(50):
            if cli.process_start_ticks(proc.pid):
                break
            time.sleep(0.05)
        real = cli.process_start_ticks(proc.pid)
        self.write_pid(cfg, proc.pid, str(int(real) + 1))
        self.assertIsNone(cli.controller_pid(cfg))
        self.write_pid(cfg, proc.pid, real)
        self.assertEqual(cli.controller_pid(cfg), proc.pid)

    def test_stop_never_signals_an_unverified_pid(self):
        cfg = self.cfg()
        self.write_pid(cfg, os.getpid(), cli.process_start_ticks(os.getpid()))
        with mock.patch.object(cli.os, "kill", side_effect=AssertionError("must not signal")):
            code, out, _ = quiet(cli.cmd_stop, cfg)
        self.assertEqual(code, 0)
        self.assertIn("no process was signalled", out)
        self.assertFalse(cli.paths(cfg)[0].exists())  # the stale pid file is gone

    def test_stop_signals_the_verified_controller_and_waits(self):
        cfg = self.cfg()
        # a stand-in whose command line is exactly what start launches
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-m", "controller", "run",
                                 "--config", cfg["_config_path"]])
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        for _ in range(50):
            if cli.process_start_ticks(proc.pid):
                break
            time.sleep(0.05)
        self.write_pid(cfg, proc.pid, cli.process_start_ticks(proc.pid))
        self.assertEqual(cli.controller_pid(cfg), proc.pid)
        code, out, err = quiet(cli.cmd_stop, cfg)  # a zombie has no command line, so it no longer counts
        self.assertEqual(code, 0, err)
        self.assertIn("Controller stopped", out)
        self.assertEqual(proc.wait(timeout=10), -signal.SIGTERM)  # SIGTERM, not SIGKILL

    def test_status_without_a_database_has_no_side_effect(self):
        cfg = self.cfg()
        code, out, _ = quiet(cli.cmd_status, cfg)
        self.assertEqual(code, 0)
        self.assertIn("STOPPED", out)
        self.assertFalse(Path(cfg["state_dir"]).exists())

    def test_review_rejects_paths_outside_the_queue_before_any_probe(self):
        cfg = self.cfg()
        repo = Path(cfg["repo_dir"])
        (repo / "tasks" / "T-1").mkdir(parents=True)
        (repo / "tasks" / "T-1" / "task.json").write_text("{}")
        (repo / "examples").mkdir()
        (repo / "examples" / "task.json").write_text("{}")
        (repo / "tasks" / "notes.txt").write_text("x")
        outside = self.d / "outside.json"
        outside.write_text("{}")
        (repo / "tasks" / "link.json").symlink_to(outside)
        with mock.patch.object(cli, "boundary_check", side_effect=AssertionError("no probe for a bad path")):
            for task in ("../outside.json", str(outside), "tasks/link.json", "tasks/notes.txt",
                         "examples/task.json", "tasks/missing.json"):
                code, _, err = quiet(cli.cmd_review, cfg, task)
                self.assertEqual(code, 2, (task, err))

    def test_main_refuses_root(self):
        with mock.patch.object(cli.os, "geteuid", return_value=0):
            code, _, err = quiet(cli.main, ["--config", str(self.d / "c.json"), "status"])
        self.assertEqual(code, 1)
        self.assertIn("do not run as root", err)


class RequireIsolation(Tmp):
    def store(self):
        s = Store(str(self.d / "aiwos.db"))
        s.init(1, 1)
        self.addCleanup(s.db.close)
        return s

    def test_no_ai_without_a_verified_probe_even_with_no_credential(self):
        store = self.store()
        env = {k: v for k, v in os.environ.items() if k not in publishing.TOKEN_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            base = {"credential_files": [], "isolation_check_ttl_s": 600}
            allowed, why = publishing.execution_allowed({**base, "require_isolation": True}, store)
            self.assertFalse(allowed)
            self.assertIn("require_isolation", why)
            # the existing behaviour is unchanged without the flag (stub tests run unisolated)
            self.assertTrue(publishing.execution_allowed({**base, "require_isolation": False}, store)[0])
            store.set_meta(publishing.ISOLATION_KEY, {"ok": True})
            self.assertTrue(publishing.execution_allowed({**base, "require_isolation": True}, store)[0])
            store.set_meta(publishing.ISOLATION_KEY, {"ok": False, "reason": "probe failed"})
            self.assertFalse(publishing.execution_allowed({**base, "require_isolation": True}, store)[0])


class WslChecks(Tmp):
    def binfmt(self, state=None):
        d = Path(tempfile.mkdtemp(dir=self.d, prefix="binfmt-"))
        if state:
            (d / "WSLInterop").write_text(f"{state}\ninterpreter /init\n")
        return d

    def mounts(self, *lines):
        f = self.d / "mounts"
        f.write_text("\n".join(lines) + "\n")
        return f

    PLAIN = "/dev/sdc / ext4 rw,relatime 0 0"
    DRIVERS = "drivers /usr/lib/wsl/drivers 9p ro,dirsync,aname=drivers;fmask=222;dmask=222 0 0"
    DRIVE_C = r"C:\134 /mnt/c 9p rw,noatime,dirsync,aname=drvfs;path=C:\;uid=1000;gid=1000 0 0"

    def test_detects_wsl(self):
        self.assertTrue(wsl.is_wsl(self.binfmt("enabled"), self.d / "none"))
        v = self.d / "version"
        v.write_text("Linux version 6.6.36.6-microsoft-standard-WSL2")
        self.assertTrue(wsl.is_wsl(self.binfmt(), v))
        v.write_text("Linux version 6.8.0-45-generic")
        self.assertFalse(wsl.is_wsl(self.binfmt(), v))

    def test_interop_and_drive_mounts_are_holes(self):
        holes = wsl.problems(self.binfmt("enabled"), self.mounts(self.PLAIN, self.DRIVERS, self.DRIVE_C))
        self.assertEqual(len(holes), 2, holes)
        self.assertIn("interop", holes[0])
        self.assertIn("/mnt/c", holes[1])

    def test_hardened_wsl_has_no_holes(self):
        self.assertEqual(wsl.problems(self.binfmt("disabled"), self.mounts(self.PLAIN, self.DRIVERS)), [])

    def test_unreadable_mounts_fail_closed(self):
        self.assertTrue(wsl.problems(self.binfmt("disabled"), self.d / "no-mounts"))

    def test_isolation_verdict_fails_closed_on_wsl_holes(self):
        import isolation
        state = self.d / "state"
        state.mkdir()
        cfg = {"state_dir": str(state), "code_dir": str(ROOT), "repo_dir": str(self.d), "credential_files": []}
        store = Store(str(state / "aiwos.db"))
        store.init(1, 1)
        self.addCleanup(store.db.close)
        with mock.patch.object(isolation, "probe", return_value=(True, {"uid": 12345, "allowed": [], "results": {}})), \
                mock.patch.object(wsl, "is_wsl", return_value=True), \
                mock.patch.object(wsl, "problems", return_value=["Windows interop is enabled"]), \
                mock.patch.object(publishing.Path, "read_text", return_value="1"):
            ok, verdict = publishing.check_isolation(cfg, store)
        self.assertFalse(ok)
        self.assertTrue(verdict["reason"].startswith("WSL:"), verdict)


class Wrappers(unittest.TestCase):
    def test_powershell_wrapper_passes_arguments_without_a_shell(self):
        text = "\n".join(l for l in PS1.read_text().splitlines() if not l.lstrip().startswith("#"))
        for banned in ("Invoke-Expression", "iex ", "cmd /c", "bash -c"):
            self.assertNotIn(banned, text)
        self.assertIn("'--exec'", text)
        commands = re.search(r"ValidateSet\(([^)]*)\)", text).group(1)
        self.assertEqual(sorted(re.findall(r"'(\w+)'", commands)),
                         sorted(["init", "doctor", "start", "status", "stop", "sync", "review"]))

    def test_linux_wrapper_runs_the_cli(self):
        r = subprocess.run([str(ROOT / "aiwos"), "--help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("aiwos-local", r.stdout)


# ---------------------------------------------------------------- real users

def environment_problem():
    if os.geteuid() != 0:
        return "needs root to run the CLI as another user"
    for u in (CTL, AGENT):
        try:
            pwd.getpwnam(u)
        except KeyError:
            return f"user {u} missing: run deploy/isolation/setup.sh"
    r = as_ctl(["sudo", "-n", "-u", AGENT, "--", "/usr/bin/python3", str(ROOT / "scripts" / "agent-exec.py"),
                "--env-json", '{"PATH": "/usr/bin:/bin"}', "--cwd", "/", "--timeout", "10", "--", "/usr/bin/id", "-un"])
    if r.stdout.strip() != AGENT:
        return f"sudo rule {CTL} -> {AGENT} not working"
    return None


def as_ctl(argv, **kw):
    home = pwd.getpwnam(CTL).pw_dir if os.geteuid() == 0 else os.environ.get("HOME", "/")
    return subprocess.run(["setpriv", f"--reuid={CTL}", f"--regid={CTL}", "--init-groups", "--",
                           "env", f"HOME={home}", *argv], capture_output=True, text=True, **kw)


PROBLEM = environment_problem()


@unittest.skipIf(PROBLEM, PROBLEM or "")
class LocalCliIsolated(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aiwos-lcli-iso-"))
        os.chmod(self.tmp, 0o755)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        bare, seed = self.tmp / "remote.git", self.tmp / "seed"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(bare))
        (seed / "tasks").mkdir(parents=True)
        (seed / "tasks" / ".keep").write_text("")
        git(seed, "init", "-q", "-b", "main")
        git(seed, "add", "-A")
        git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
        git(seed, "push", "-q", str(bare), "main")
        git(self.tmp, "clone", "-q", str(bare), str(self.tmp / "repo"))
        for d in ("state", "ws"):
            (self.tmp / d).mkdir()
        pw = pwd.getpwnam(CTL)
        for d in ("remote.git", "repo", "state", "ws"):
            subprocess.run(["chown", "-R", f"{pw.pw_uid}:{pw.pw_gid}", str(self.tmp / d)], check=True)
        self.config = Path(pw.pw_dir) / f"test-local-{secrets.token_hex(4)}.json"
        self.addCleanup(lambda: self.config.unlink(missing_ok=True))
        self.addCleanup(self.kill_leftovers)

    def cli(self, *args, **kw):
        return as_ctl(["/usr/bin/python3", str(ROOT / "scripts" / "aiwos-local.py"), "--config", str(self.config),
                       *args], cwd="/", **kw)

    def init(self, *extra):
        r = self.cli("init", "--repo-dir", str(self.tmp / "repo"), "--state-dir", str(self.tmp / "state"),
                     "--workspaces-dir", str(self.tmp / "ws"), "--agent-user", AGENT, "--agent-group", GROUP, *extra)
        self.assertEqual(r.returncode, 0, r.stderr)

    def procs(self, kind):
        r = subprocess.run(["pgrep", "-u", CTL, "-f", f"controller {kind} --config {self.config}"],
                           capture_output=True, text=True)
        return [int(p) for p in r.stdout.split()]

    def wait_for(self, cond, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.3)
        return False

    def kill_leftovers(self):
        for kind in ("run", "worker"):
            for pid in self.procs(kind):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_start_status_stop_and_restart_after_a_crash(self):
        self.init()
        r = self.cli("sync")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"added": []', r.stdout)  # nothing queued: no model can start

        r = self.cli("start")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.wait_for(lambda: len(self.procs("worker")) == 10), "10 workers did not start")
        r = self.cli("status")
        self.assertIn("RUNNING", r.stdout)
        self.assertIn("Isolation: ok=True", r.stdout)
        self.assertIn("model slots: 0/1", r.stdout)
        # a second start does not start a second controller
        self.cli("start")
        self.assertEqual(len(self.procs("run")), 1)
        # review is refused while the controller runs
        r = self.cli("review", "tasks/x/task.json")
        self.assertEqual(r.returncode, 2)

        # crash: the controller is killed outright; its workers must not live on
        os.kill(self.procs("run")[0], signal.SIGKILL)
        self.assertTrue(self.wait_for(lambda: not self.procs("worker"), 60), "workers outlived the controller")
        r = self.cli("status")
        self.assertIn("STOPPED", r.stdout)  # the stale pid file is not trusted
        r = self.cli("start")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.wait_for(lambda: len(self.procs("worker")) == 10))
        self.assertEqual(len(self.procs("run")), 1)

        r = self.cli("stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.wait_for(lambda: not self.procs("run") and not self.procs("worker"), 30))

    def test_start_refuses_when_the_agent_can_read_a_credential(self):
        leaked = self.tmp / "gh-token.env"
        leaked.write_text("GH_TOKEN=" + "ghp_" + secrets.token_hex(20) + "\n")  # sentinel, never real
        os.chmod(leaked, 0o644)  # readable by the agent user: isolation is NOT proven
        self.init("--credential-file", str(leaked))
        r = self.cli("start")
        self.assertEqual(r.returncode, 1)
        self.assertIn("OS isolation not verified", r.stderr)
        self.assertNotIn(leaked.read_text().strip(), r.stdout + r.stderr)  # never printed
        self.assertEqual(self.procs("run"), [])
        self.assertEqual(self.procs("worker"), [])
        r = self.cli("sync")
        self.assertEqual(r.returncode, 1)

    def test_doctor_reports_without_starting_anything(self):
        self.init()
        r = self.cli("doctor")
        self.assertIn("sudo rule", r.stdout)
        self.assertIn("[OK] sudo rule", r.stdout)
        self.assertEqual(self.procs("run"), [])


if __name__ == "__main__":
    unittest.main()
