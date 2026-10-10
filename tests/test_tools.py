"""Tests for scan_secrets.py, doctor.sh and render-brief.py.

Run: python3 -m unittest discover -s tests -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import scan_secrets  # noqa: E402

# Fake credentials are assembled at runtime so this file never contains one.
FAKES = {
    "github-token": "ghp_" + "a1B2" * 9,
    "anthropic-key": "sk-ant-" + "x" * 30,
    "google-api-key": "AIza" + "B" * 35,
    "aws-access-key-id": "AKIA" + "C" * 16,
    "private-key-block": "-----BEGIN RSA " + "PRIVATE KEY-----",
}


class ScanSecrets(unittest.TestCase):
    def test_each_pattern_fires(self):
        for name, value in FAKES.items():
            with self.subTest(name=name):
                self.assertIn((1, name), scan_secrets.find_in_text(f"x = '{value}'"))

    def test_ordinary_text_is_clean(self):
        text = "GEMINI_API_KEY is read from the environment\nsk-short\nghp_ too short"
        self.assertEqual(scan_secrets.find_in_text(text), [])

    def test_cli_never_prints_the_secret(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "leak.txt"
            f.write_text("token=" + FAKES["github-token"] + "\n", encoding="utf-8")
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / "scan_secrets.py"), str(f)],
                               capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("github-token", r.stdout)
        self.assertNotIn(FAKES["github-token"], r.stdout + r.stderr)

    def test_repository_is_clean(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "scan_secrets.py")],
                           cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)


class Doctor(unittest.TestCase):
    def run_doctor(self, env):
        return subprocess.run(["bash", str(ROOT / "scripts" / "doctor.sh")],
                              cwd=ROOT, capture_output=True, text=True, env=env, timeout=120)

    def test_runs_and_reports_required_items(self):
        r = self.run_doctor(dict(os.environ))
        self.assertIn(r.returncode, (0, 1))
        for item in ("git", "python3", "jsonschema", "gh", "claude", "codex"):
            self.assertRegex(r.stdout, rf"(?m)^{item}\s")

    def test_does_not_print_tokens(self):
        sentinel = "doctor-sentinel-" + "z" * 24
        env = dict(os.environ, GH_TOKEN=sentinel, GITHUB_TOKEN=sentinel, ANTHROPIC_API_KEY=sentinel,
                   OPENAI_API_KEY=sentinel)
        r = self.run_doctor(env)
        self.assertNotIn(sentinel, r.stdout + r.stderr)

    @unittest.skipUnless(shutil.which("git") and shutil.which("python3"), "needs git and python3")
    def test_fails_when_required_item_missing(self):
        # A PATH holding only bash and coreutils hides git and python3.
        with tempfile.TemporaryDirectory() as d:
            for tool in ("bash", "date", "dirname", "head", "cut", "grep", "timeout", "printf"):
                src = shutil.which(tool)
                if src:
                    os.symlink(src, Path(d) / tool)
            r = subprocess.run([str(Path(d) / "bash"), str(ROOT / "scripts" / "doctor.sh")],
                               cwd=ROOT, capture_output=True, text=True, env={"PATH": d}, timeout=60)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("MISSING", r.stdout)


class RenderBrief(unittest.TestCase):
    def run_brief(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "render-brief.py"), *args],
                              cwd=ROOT, capture_output=True, text=True)

    def test_worker_brief_for_both_samples(self):
        for path, agent in (("examples/development/task.json", "claude-code"),
                            ("examples/document-compare/task.json", "codex-cli")):
            with self.subTest(path=path):
                r = self.run_brief(path)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn(f"(for: {agent})", r.stdout)
                self.assertIn("AC1", r.stdout)
                self.assertIn("FC1", r.stdout)

    def test_reviewer_brief_addresses_reviewer(self):
        r = self.run_brief("examples/development/task.json", "--role", "reviewer")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("(for: codex-cli)", r.stdout)
        self.assertIn("UNVERIFIED", r.stdout)

    def test_invalid_task_gets_no_brief(self):
        r = self.run_brief("tests/fixtures/invalid-task.json")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")

    def test_draft_is_not_workable(self):
        r = self.run_brief("contracts/task.example.json")
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
