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


def cli_wait(cond, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


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
        cfg = {"state_dir": str(self.d / "state"), "workspaces_dir": str(self.d / "ws"), "workers": 10,
               "model_concurrency": 1,
               "repo_dir": str(self.d / "repo"), "code_dir": str(ROOT), "_config_path": str(self.d / "local.json"),
               "publish": "none", "require_isolation": True, "agent_user": "nobody-agent",
               "agent_group": "nobody-group", "task_globs": ["tasks/DUMMY-0001/task.json"], "allowed_task_ids": ["DUMMY-0001"], "kill_grace_s": 1}
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
        self.assertEqual(data["task_globs"], ["tasks/DUMMY-0001/task.json"])  # the dummy contract only
        self.assertEqual(data["allowed_task_ids"], ["DUMMY-0001"])
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

    def test_local_policy_insists_on_10_workers_1_model_and_the_dummy_task_only(self):
        self.assertEqual(cli.local_policy(self.cfg()), [])
        for bad in (dict(workers=9), dict(workers=1), dict(model_concurrency=2), dict(model_concurrency=0)):
            self.assertTrue(cli.local_policy(self.cfg(**bad)), bad)
        for globs in ([], "tasks/DUMMY-0001/task.json", ["tasks/*.json", "tasks/*/*.json"], ["tasks/*/task.json"],
                      ["examples/*/task*.json"], ["tasks/DUMMY-0001/task.json", "tasks/REAL-0001/task.json"],
                      ["tasks/../examples/x.json"], ["tasks/DUMMY-000?/task.json"], [None]):
            self.assertTrue(cli.local_policy(self.cfg(task_globs=globs)), globs)
        for ids in (None, [], ["REAL-0001"], ["DUMMY-0001", "REAL-0001"], "DUMMY-0001", ["dummy-0001"]):
            self.assertTrue(cli.local_policy(self.cfg(allowed_task_ids=ids)), ids)

    def test_start_sync_and_review_refuse_a_config_outside_the_local_policy(self):
        with mock.patch("controller.main.refresh_isolation", side_effect=AssertionError("probe must not run")), \
                mock.patch.object(cli, "unsupervised", return_value={}), \
                mock.patch.object(cli.subprocess, "Popen", side_effect=AssertionError("nothing may start")):
            for bad in (self.cfg(workers=3), self.cfg(model_concurrency=2), self.cfg(task_globs=["tasks/*.json"]),
                        self.cfg(allowed_task_ids=None), self.cfg(allowed_task_ids=["REAL-0001"])):
                for cmd in (cli.cmd_start, cli.cmd_sync):
                    with self.assertRaises(cli.Refused):
                        cmd(bad)

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
                                 "--config", cfg["_config_path"]], cwd=ROOT)
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
        self.assertIn("no controller was signalled", out)
        self.assertFalse(cli.paths(cfg)[0].exists())  # the stale pid file is gone

    def test_stop_signals_the_verified_controller_and_waits(self):
        cfg = self.cfg()
        # a stand-in whose command line is exactly what start launches
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-m", "controller", "run",
                                 "--config", cfg["_config_path"]], cwd=ROOT)
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

    def stand_in(self, env=None, argv=None, cwd=ROOT, code="import time; time.sleep(60)"):
        proc = subprocess.Popen(argv or [sys.executable, "-c", code], env={**os.environ, **(env or {})}, cwd=cwd)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        for _ in range(50):
            if cli.process_start_ticks(proc.pid):
                break
            time.sleep(0.05)
        return proc

    def test_finds_workers_and_stage_runners_of_this_config_only(self):
        cfg = self.cfg()
        wt = Path(cfg["state_dir"]) / "worktrees" / "w01"
        wt.mkdir(parents=True)
        worker = self.stand_in(argv=[sys.executable, "-c", "import time; time.sleep(60)", "-m", "controller",
                                     "worker", "--config", cfg["_config_path"], "--id", "1"])
        stage = self.stand_in(env={"AIWOS_LEASE_TAG": "T-1:1", "AIWOS_ROOT": str(wt)})
        other_cfg = self.stand_in(argv=[sys.executable, "-c", "import time; time.sleep(60)", "-m", "controller",
                                        "worker", "--config", str(self.d / "other.json"), "--id", "1"])
        other_stage = self.stand_in(env={"AIWOS_LEASE_TAG": "T-9:9", "AIWOS_ROOT": str(self.d / "elsewhere")})
        # look-alikes: the right words in one argument, or the right argv run from another directory
        one_arg = self.stand_in(argv=[sys.executable, "-c", "import time; time.sleep(60)",
                                      f"-m controller worker --config {cfg['_config_path']}"])
        elsewhere = self.stand_in(argv=[sys.executable, "-c", "import time; time.sleep(60)", "-m", "controller",
                                        "worker", "--config", cfg["_config_path"], "--id", "2"], cwd=self.d)
        found = cli.config_processes(cfg)
        self.assertEqual(found[worker.pid].kind, "worker")
        self.assertEqual(found[stage.pid].kind, "stage")
        self.assertEqual(found[stage.pid].ticks, cli.process_start_ticks(stage.pid))
        for unrelated in (other_cfg, other_stage, one_arg, elsewhere):
            self.assertNotIn(unrelated.pid, found)
        self.assertEqual(cli.unsupervised(cfg), found)  # no controller in the pid file: all of it is unsupervised

    def test_stop_ends_leftovers_stage_first_with_sigint(self):
        cfg = self.cfg()
        left = {101: cli.Proc("stage", "7"), 102: cli.Proc("worker", "8")}
        calls = []
        scans, calls = [], []
        with mock.patch.object(cli, "unsupervised", side_effect=lambda c: scans.append(1) or (left if len(scans) <= 2 else {})), \
                mock.patch.object(cli, "stop_process",
                                  side_effect=lambda pid, proc, first, grace, escalate:
                                  calls.append((pid, first)) or cli.Stop("stopped", [])):
            code, out, err = quiet(cli.cmd_stop, cfg)
        self.assertEqual(code, 0, err)
        self.assertIn("WARNING: running without a controller", out)
        self.assertEqual(sorted(calls), [(101, signal.SIGINT), (102, signal.SIGTERM)])

    def test_stop_reports_what_it_could_not_end(self):
        cfg = self.cfg()
        target = self.stand_in()
        ticks = cli.process_start_ticks(target.pid)
        left = {target.pid: cli.Proc("stage", ticks)}
        with mock.patch.object(cli, "unsupervised", return_value=left), mock.patch.object(cli, "AGENT_STOP_S", 0), \
                mock.patch.object(cli, "stop_process", return_value=cli.Stop("running", [(target.pid, ticks)])):
            code, _, err = quiet(cli.cmd_stop, cfg)
        self.assertEqual(code, 1)
        self.assertIn("left running", err)
        self.assertEqual(cli.live_survivors(cfg), [(target.pid, ticks)])

    def test_stop_uses_the_recorded_start_time_for_the_controller(self):
        cfg = self.cfg()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-m", "controller", "run",
                                 "--config", cfg["_config_path"]], cwd=ROOT)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        cli_wait(lambda: cli.process_start_ticks(proc.pid), 5)
        recorded = cli.process_start_ticks(proc.pid)
        self.write_pid(cfg, proc.pid, recorded)
        seen = []
        # the PID is taken over by another process right after it was verified:
        # /proc now shows another start time, the check must still use the recorded one
        with mock.patch.object(cli, "controller_pid", return_value=proc.pid), \
                mock.patch.object(cli, "process_start_ticks", return_value="999999999"), \
                mock.patch.object(cli, "stop_process", side_effect=lambda pid, p, *a, **k: seen.append(p) or cli.Stop("stopped", [])), \
                mock.patch.object(cli, "unsupervised", return_value={}):
            quiet(cli.cmd_stop, cfg)
        self.assertEqual(seen, [cli.Proc("controller", recorded)])

    def test_a_reused_pid_is_never_signalled(self):
        # the process seen earlier is gone and its PID now belongs to another
        # process of the same user: its start time differs, so nothing is sent
        bystander = self.stand_in()
        seen = cli.Proc("stage", str(int(cli.process_start_ticks(bystander.pid)) - 1))
        self.assertEqual(cli.stop_process(bystander.pid, seen, signal.SIGINT, 0.5, escalate=True).state, "gone")
        time.sleep(0.3)
        self.assertIsNone(bystander.poll(), "an unrelated process was signalled")
        cfg = self.cfg()
        calls = []
        with mock.patch.object(cli, "unsupervised",
                               side_effect=lambda c: calls.append(1) or ({bystander.pid: seen} if len(calls) <= 2 else {})):
            code, _, err = quiet(cli.cmd_stop, cfg)
        self.assertEqual(code, 0, err)
        self.assertIsNone(bystander.poll(), "stop signalled a reused PID")

    def test_signals_go_through_a_pidfd_or_not_at_all(self):
        target = self.stand_in()
        proc = cli.Proc("stage", cli.process_start_ticks(target.pid))
        with mock.patch.object(cli.os, "pidfd_open", side_effect=OSError(38, "Function not implemented")):
            with self.assertRaisesRegex(cli.Refused, "pidfd"):
                cli.stop_process(target.pid, proc, signal.SIGINT, 0.5, escalate=True)
        self.assertIsNone(target.poll())
        with mock.patch.object(cli.os, "kill", side_effect=AssertionError("plain kill(2) must not be used")):
            self.assertEqual(cli.stop_process(target.pid, proc, signal.SIGINT, 5, escalate=True).state, "stopped")
        self.assertEqual(target.wait(timeout=10), -signal.SIGINT)

    def test_escalates_to_sigkill_for_a_stage_that_ignores_sigint(self):
        child_code = "import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(60)"
        code = ("import signal,subprocess,sys,time; signal.signal(signal.SIGINT, signal.SIG_IGN); "
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); time.sleep(60)")
        top = self.stand_in(code=code)
        time.sleep(0.5)
        children = [int(e) for e in os.listdir("/proc") if e.isdigit() and cli._ppid(int(e)) == top.pid]
        self.assertEqual(len(children), 1)
        proc = cli.Proc("stage", cli.process_start_ticks(top.pid))
        self.assertEqual(cli.stop_process(top.pid, proc, signal.SIGINT, 0.5, escalate=True).state, "killed")
        self.assertEqual(top.wait(timeout=10), -signal.SIGKILL)
        from controller.store import pid_alive
        self.assertTrue(cli_wait(lambda: not pid_alive(children[0]), 10), "the child was left running")

    # ---- a stage tree whose top ends before its children (GPT re-review of 6c0b456)

    TOP_EXITS_ON_SIGINT = ("import signal,subprocess,sys,time\n"
                           "child = subprocess.Popen(sys.argv[1:])\n"
                           "signal.signal(signal.SIGINT, lambda *a: sys.exit(0))\n"
                           "time.sleep(60)\n")
    STUBBORN = "import signal,time; [signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGTERM)]; time.sleep(60)"

    def tree(self, child_argv):
        top = self.stand_in(argv=[sys.executable, "-c", self.TOP_EXITS_ON_SIGINT, *child_argv])
        self.assertTrue(cli_wait(lambda: any(cli._ppid(int(e)) == top.pid for e in os.listdir("/proc")
                                             if e.isdigit()), 10), "the child did not start")
        child = next(int(e) for e in os.listdir("/proc") if e.isdigit() and cli._ppid(int(e)) == top.pid)
        self.addCleanup(lambda: subprocess.run(["kill", "-KILL", str(child)], capture_output=True))
        return top, child

    def test_children_that_outlive_their_runner_are_stopped_not_reported_stopped(self):
        from controller.store import pid_alive
        top, child = self.tree([sys.executable, "-c", self.STUBBORN])
        r = cli.stop_process(top.pid, cli.Proc("stage", cli.process_start_ticks(top.pid)), signal.SIGINT, 0.5,
                             escalate=True)
        self.assertNotEqual(getattr(r, "state", r), "stopped", "success while a child was still running")
        self.assertTrue(cli_wait(lambda: not pid_alive(child), 10), "the child of the runner was left running")

    def test_descendants_are_pinned_before_the_signal_never_listed_after(self):
        # the runner exits on SIGINT and its PID is (in effect) reused: a later
        # listing would show another process tree under that PID
        from controller.store import pid_alive
        top, child = self.tree([sys.executable, "-c", "import time; time.sleep(60)"])
        bystander = self.stand_in(code="import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)")
        real_ppid = cli._ppid

        def ppid(pid):  # once the runner is gone, the bystander "is its child"
            if pid == bystander.pid and top.poll() is not None:
                return top.pid
            return real_ppid(pid)
        with mock.patch.object(cli, "_ppid", side_effect=ppid), \
                mock.patch.object(cli, "exited", side_effect=lambda fd, s: time.sleep(0.5) or False, create=True):
            cli.stop_process(top.pid, cli.Proc("stage", cli.process_start_ticks(top.pid)), signal.SIGINT, 0.5,
                             escalate=True)
        time.sleep(0.3)
        self.assertIsNone(bystander.poll(), "a process outside the pinned tree was signalled")
        self.assertTrue(cli_wait(lambda: not pid_alive(child), 10))

    def test_a_process_that_is_no_longer_a_child_when_pinned_is_left_alone(self):
        # listed as a child, but by the time its pidfd is open its parent is another
        # process (the listed PID was reused): it is not part of the tree, never signalled
        top, child = self.tree([sys.executable, "-c", self.STUBBORN])
        real_ppid, seen = cli._ppid, []

        def ppid(pid):
            if pid == child:
                seen.append(1)
                return top.pid if len(seen) == 1 else 1
            return real_ppid(pid)
        with mock.patch.object(cli, "_ppid", side_effect=ppid):
            cli.stop_process(top.pid, cli.Proc("stage", cli.process_start_ticks(top.pid)), signal.SIGINT, 0.5,
                             escalate=True)
        time.sleep(0.3)
        self.assertTrue(cli.still_running(child, cli.process_start_ticks(child)), "a re-parented process was signalled")

    @unittest.skipIf(os.geteuid() != 0, "needs root to run a child as another user")
    def test_another_users_process_that_survives_fails_closed_and_blocks_restart(self):
        from controller.store import pid_alive
        cfg = self.cfg()
        Path(cfg["state_dir"]).mkdir(parents=True)
        other = ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "--",
                 sys.executable, "-c", self.STUBBORN]
        top, child = self.tree(other)
        r = cli.stop_process(top.pid, cli.Proc("stage", cli.process_start_ticks(top.pid)), signal.SIGINT, 0.5,
                             escalate=True)
        self.assertEqual(getattr(r, "state", r), "running", "success while another user's process was left")
        self.assertTrue(pid_alive(child))  # never signalled: it is not ours to stop
        # through stop: fail closed, and start is refused while it lives
        top2, child2 = self.tree(other)
        with mock.patch.object(cli, "unsupervised",
                               side_effect=lambda c: {top2.pid: cli.Proc("stage", cli.process_start_ticks(top2.pid))}
                               if top2.poll() is None else {}):
            code, _, err = quiet(cli.cmd_stop, cfg)
        self.assertEqual(code, 1, err)
        self.assertTrue(pid_alive(child2))
        with mock.patch.object(cli, "unsupervised", return_value={}), \
                mock.patch.object(cli, "controller_pid", return_value=None):
            with self.assertRaisesRegex(cli.Refused, "still running"):
                cli.refuse_if_busy(cfg, "start")
            subprocess.run(["kill", "-KILL", str(child), str(child2)], capture_output=True)
            self.assertTrue(cli_wait(lambda: not pid_alive(child2) and not pid_alive(child), 10))
            cli.refuse_if_busy(cfg, "start")  # gone: allowed again

    @unittest.skipIf(os.geteuid() != 0, "needs root to run a stand-in as another user")
    def test_agent_side_processes_are_found_and_never_signalled(self):
        from controller.store import pid_alive
        cfg = self.cfg()
        tree_dir = Path(cfg["workspaces_dir"]) / "w01" / "tree"
        tree_dir.mkdir(parents=True)
        agent = self.stand_in(argv=["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", "--",
                                    sys.executable, "-c", "import time; time.sleep(60)",
                                    str(ROOT / "scripts" / "agent-exec.py"), "--cwd", str(tree_dir)])
        self.assertTrue(cli_wait(lambda: any(p.kind == "agent" for p in cli.config_processes(cfg).values()), 10),
                        cli.config_processes(cfg))
        code, _, err = quiet(cli.cmd_stop, cfg)
        self.assertEqual(code, 1, err)  # cannot be stopped by this user: fail closed
        self.assertIsNone(agent.poll(), "an agent-side process was signalled directly")
        with self.assertRaises(cli.Refused):
            cli.refuse_if_busy(cfg, "start")
        agent.kill()
        agent.wait()
        self.assertTrue(cli_wait(lambda: not cli.unsupervised(cfg), 10))

    def test_status_warns_about_unsupervised_processes(self):
        cfg = self.cfg()
        with mock.patch.object(cli, "unsupervised", return_value={4242: cli.Proc("stage", "7")}):
            code, out, _ = quiet(cli.cmd_status, cfg)
        self.assertIn("WARNING: running without a controller: stage 4242", out)

    def test_sync_waits_for_the_operator_lock_and_refuses_beside_a_controller(self):
        import fcntl
        import threading
        cfg = self.cfg()
        Path(cfg["state_dir"]).mkdir(parents=True)
        # refused while a controller runs: no probe, no git
        with mock.patch.object(cli, "controller_pid", return_value=4242), \
                mock.patch.object(cli, "boundary_check", side_effect=AssertionError("no probe")), \
                mock.patch.object(cli, "run_controller", side_effect=AssertionError("no git")):
            with self.assertRaisesRegex(cli.Refused, "sync is refused while the controller runs"):
                cli.cmd_sync(cfg)
        # serialised with start/stop/review through the operator lock
        done = threading.Event()
        lock = os.open(str(cli.paths(cfg)[2]), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with mock.patch.object(cli, "unsupervised", return_value={}), \
                    mock.patch.object(cli, "boundary_check"), \
                    mock.patch.object(cli, "run_controller", side_effect=lambda *a, **k: done.set() or 0):
                t = threading.Thread(target=cli.cmd_sync, args=(cfg,))
                t.start()
                self.assertFalse(done.wait(0.7))  # blocked behind the lock
                fcntl.flock(lock, fcntl.LOCK_UN)
                self.assertTrue(done.wait(10))
                t.join(10)
        finally:
            os.close(lock)

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

    def test_review_runs_only_the_dummy_task_id(self):
        cfg = self.cfg()
        contract = Path(cfg["repo_dir"]) / "tasks" / "DUMMY-0001" / "task.json"
        contract.parent.mkdir(parents=True)

        class Reached(Exception):
            pass
        for task_id in ("REAL-0001", "dummy-0001", "DUMMY-0001 ", None, 1):
            contract.write_text(json.dumps({"task_id": task_id}))
            with mock.patch.object(cli, "boundary_check", side_effect=AssertionError("no probe for another id")):
                code, _, err = quiet(cli.cmd_review, cfg, "tasks/DUMMY-0001/task.json")
            self.assertEqual(code, 2, task_id)
            self.assertIn("only DUMMY-0001", err)
        contract.write_text("not json")
        self.assertEqual(quiet(cli.cmd_review, cfg, "tasks/DUMMY-0001/task.json")[0], 2)
        contract.write_text(json.dumps({"task_id": "DUMMY-0001"}))  # the real one gets as far as the probe
        with mock.patch.object(cli, "unsupervised", return_value={}), \
                mock.patch.object(cli, "boundary_check", side_effect=Reached):
            with self.assertRaises(Reached):
                cli.cmd_review(cfg, "tasks/DUMMY-0001/task.json")

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


class AllowedTaskIds(unittest.TestCase):
    """controller: allowed_task_ids keeps every other task out of the queue (sync)
    and from running (worker), whatever path its contract sits at."""

    def make(self, **kw):
        from controller_harness import Sandbox
        sb = Sandbox(**kw)
        self.addCleanup(sb.close)
        return sb

    def test_config_validates_the_list(self):
        from controller.config import ConfigError, load
        d = Path(tempfile.mkdtemp(prefix="aiwos-allow-"))
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        for bad in ([], "DUMMY-0001", [""], [1]):
            (d / "c.json").write_text(json.dumps({"repo_dir": str(d), "state_dir": str(d / "s"),
                                                   "allowed_task_ids": bad}))
            with self.assertRaises(ConfigError, msg=bad):
                load(d / "c.json")

    def test_sync_queues_only_allowed_ids(self):
        sb = self.make(n_tasks=3, config={"allowed_task_ids": ["T-0002"], "publish": "none"})
        r = sb.cli("sync-once")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(set(sb.tasks()), {"T-0002"})
        skipped = [json.loads(e["detail"])["path"] for e in sb.events("sync_skipped")]
        self.assertEqual(sorted(skipped), ["tasks/T-0001/task.json", "tasks/T-0003/task.json"])

    def test_worker_never_runs_a_queued_task_outside_the_list(self):
        sb = self.make(n_tasks=2, workers=2, model_concurrency=1, config={"publish": "none"})
        self.assertEqual(sb.cli("sync-once").returncode, 0)  # both queued before the list existed
        self.assertEqual(set(sb.tasks()), {"T-0001", "T-0002"})
        cfg = json.loads(sb.config.read_text())
        cfg["allowed_task_ids"] = ["T-0001"]
        sb.config.write_text(json.dumps(cfg))
        sb.start()
        self.assertTrue(sb.wait_for(lambda: sb.tasks()["T-0002"]["state"] == "held", timeout=60), sb.tasks())
        self.assertTrue(sb.wait_for(lambda: sb.tasks()["T-0001"]["ai_runs"] >= 1, timeout=120), sb.tasks())
        sb.stop()
        t2 = sb.tasks()["T-0002"]
        self.assertIn("allowed_task_ids", t2["reason"])
        self.assertEqual(t2["ai_runs"], 0)
        self.assertFalse([c for c in sb.spy_calls() if "T-0002" in c["cwd"]], "a model ran for T-0002")


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

    def test_guide_creates_the_users_before_running_anything_as_them(self):
        lines = (ROOT / "docs" / "local-cli.md").read_text().splitlines()
        setup = next(i for i, l in enumerate(lines) if "sudo bash" in l and "deploy/isolation/setup.sh" in l)
        as_users = [i for i, l in enumerate(lines) if re.search(r"sudo -u aiwos-(agent|ctl)", l)]
        self.assertTrue(as_users)
        early = [lines[i] for i in as_users if i < setup]
        self.assertEqual(early, [], "sudo -u aiwos-* before setup.sh creates the users")

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

# plays claude and codex: logged in, and a work stage that never finishes on its own
SLOW_STUB = """#!/usr/bin/env python3
import json, os, sys, time
cli = os.path.basename(__file__).split("-")[-1]
args = sys.argv[1:]
if args == ["--version"]:
    print("0 (stub " + cli + ")"); sys.exit(0)
if cli == "claude" and args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "oauth_token", "apiProvider": "firstParty"})); sys.exit(0)
if cli == "codex" and args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT"); sys.exit(0)
sys.stdin.read()
time.sleep(600)
"""


@unittest.skipIf(PROBLEM, PROBLEM or "")
class LocalCliIsolated(unittest.TestCase):
    def setUp(self):
        self.build()

    def build(self, dummy_task=False, dummy_path_id="DUMMY-0001", real_task=False):
        """A local bare remote and a data clone; with dummy_task, the dummy
        contract is queued and the agent CLIs are stubs (no model) whose work
        stage just sleeps, standing in for an AI that is still working."""
        if getattr(self, "tmp", None):
            shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="aiwos-lcli-iso-"))
        os.chmod(self.tmp, 0o755)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        bare, seed = self.tmp / "remote.git", self.tmp / "seed"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(bare))
        (seed / "tasks").mkdir(parents=True)
        (seed / "tasks" / ".keep").write_text("")
        if dummy_task:
            (seed / "tasks" / "DUMMY-0001").mkdir()
            contract = json.loads((ROOT / "examples" / "local-cli" / "dummy-task.json").read_text())
            contract["task_id"] = dummy_path_id  # another id at the dummy's path: must not run
            (seed / "tasks" / "DUMMY-0001" / "task.json").write_text(json.dumps(contract, ensure_ascii=False))
            if real_task:  # a real-project-looking task beside it
                (seed / "tasks" / "REAL-0002").mkdir()
                contract["task_id"] = "REAL-0002"
                (seed / "tasks" / "REAL-0002" / "task.json").write_text(json.dumps(contract, ensure_ascii=False))
            bins = self.tmp / "bin"
            bins.mkdir()
            for cli_name in ("claude", "codex"):
                (bins / f"stub-{cli_name}").write_text(SLOW_STUB)
                (bins / f"stub-{cli_name}").chmod(0o755)
            self.bins = bins
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
        if getattr(self, "config", None) is None:
            self.config = Path(pw.pw_dir) / f"test-local-{secrets.token_hex(4)}.json"
            self.addCleanup(lambda: self.config.unlink(missing_ok=True))
            self.addCleanup(self.kill_leftovers)
        self.config.unlink(missing_ok=True)

    def use_stubs(self):
        """Point the config at the stub CLIs and make the controller quick (as root,
        keeping the file the controller user's, mode 0600)."""
        data = json.loads(self.config.read_text())
        data.update(agent_bins={"claude": str(self.bins / "stub-claude"), "codex": str(self.bins / "stub-codex")},
                    poll_interval_s=0.5, sync_interval_s=5, heartbeat_s=2, lease_ttl_s=30, kill_grace_s=5)
        self.config.write_text(json.dumps(data))

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
        subprocess.run(["pkill", "-KILL", "-u", AGENT], capture_output=True)
        subprocess.run(["pkill", "-KILL", "-u", CTL, "-f", "scripts/aiwos.py run"], capture_output=True)

    @staticmethod
    def agent_procs():
        r = subprocess.run(["pgrep", "-u", AGENT], capture_output=True, text=True)
        return [int(p) for p in r.stdout.split()]

    @staticmethod
    def stage_runners():
        r = subprocess.run(["pgrep", "-u", CTL, "-f", "scripts/aiwos.py run"], capture_output=True, text=True)
        return [int(p) for p in r.stdout.split()]

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
        # review needs an existing contract; sync is refused while the controller runs
        r = self.cli("review", "tasks/x/task.json")
        self.assertEqual(r.returncode, 2)
        r = self.cli("sync")
        self.assertEqual(r.returncode, 1)
        self.assertIn("sync is refused while the controller runs", r.stderr)

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

    def test_stop_ends_an_ai_stage_left_running_by_a_crash(self):
        self.build(dummy_task=True)
        self.init()
        self.use_stubs()
        r = self.cli("start")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.wait_for(lambda: self.agent_procs() and self.stage_runners(), 120),
                        "the stub agent never started")
        runner = self.stage_runners()[0]
        worker = cli._ppid(runner)
        # power-cut-like: the worker and the controller die at once, the AI stage does not
        os.kill(worker, signal.SIGKILL)
        os.kill(self.procs("run")[0], signal.SIGKILL)
        time.sleep(3)
        self.assertTrue(self.agent_procs(), "precondition: the AI stage outlived its controller")

        r = self.cli("status")
        self.assertIn("STOPPED", r.stdout)
        self.assertIn("WARNING: running without a controller", r.stdout)
        for cmd in ("start", "sync"):  # nothing new starts beside an unsupervised AI
            r = self.cli(cmd)
            self.assertEqual(r.returncode, 1, cmd)
            self.assertIn("run stop first", r.stderr)
        self.assertEqual(self.procs("run"), [])

        r = self.cli("stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WARNING: running without a controller", r.stdout)
        self.assertIn("Stopped leftover processes", r.stdout)
        self.assertTrue(self.wait_for(lambda: not self.agent_procs() and not self.stage_runners(), 30),
                        f"left: agent {self.agent_procs()} runners {self.stage_runners()}")
        r = self.cli("status")
        self.assertNotIn("WARNING", r.stdout)

    def test_only_the_dummy_id_ever_reaches_a_model(self):
        self.build(dummy_task=True, dummy_path_id="REAL-0001", real_task=True)
        self.init()
        self.use_stubs()
        r = self.cli("sync")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"added": []', r.stdout)  # neither the disguised nor the real task is queued
        r = self.cli("review", "tasks/DUMMY-0001/task.json")
        self.assertEqual(r.returncode, 2)
        self.assertIn("only DUMMY-0001", r.stderr)
        r = self.cli("review", "tasks/REAL-0002/task.json")
        self.assertEqual(r.returncode, 2)
        r = self.cli("start")
        self.assertEqual(r.returncode, 0, r.stderr)
        time.sleep(12)  # several sync and claim rounds (sync every 5 s, poll every 0.5 s)
        self.assertEqual(self.agent_procs(), [], "a model process started")
        self.assertEqual(self.stage_runners(), [])
        r = self.cli("status")
        self.assertIn("tasks (0):", r.stdout)
        self.assertEqual(self.cli("stop").returncode, 0)

    def test_doctor_reports_without_starting_anything(self):
        self.init()
        r = self.cli("doctor")
        self.assertIn("sudo rule", r.stdout)
        self.assertIn("[OK] sudo rule", r.stdout)
        self.assertEqual(self.procs("run"), [])


if __name__ == "__main__":
    unittest.main()
