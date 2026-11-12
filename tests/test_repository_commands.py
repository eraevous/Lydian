"""repository-commands: the repo's own non-unittest suites run inside kairos-checks.

Added 2026-11-12 after Zach found relay-kit's required check skipped its main suite
(349 cases in tests/run_tests.py, plus node browser tests). Each case below is a rule the
module promises: what runs, what fails, what the report may and may not contain.
"""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kit_run_cmd", ROOT / "kit/run.py")
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)

POLICY = {"optional": True, "timeout_seconds": 30, "allowed_programs": ["{python}", "node"]}


class RepositoryCommands(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        (self.repo / "ok.py").write_text("print('fine')\n")
        (self.repo / "bad.py").write_text("print('SECRET-TOKEN-abc123'); raise SystemExit(3)\n")
        self.suite = {"schema": 1, "profile": "p", "modules": {"repository-commands": dict(POLICY)}}

    def adapter(self, commands=None):
        modules = {} if commands is None else {"repository-commands": {"commands": commands}}
        return {"schema": 1, "profile": "p", "modules": modules}

    def run_kit(self, commands=None):
        return kit.evaluate(self.repo, self.adapter(commands), self.suite)

    def test_declared_commands_run_and_count(self):
        r = self.run_kit([{"name": "a", "argv": ["{python}", "-I", "ok.py"]},
                          {"name": "b", "argv": ["{python}", "-I", "ok.py"]}])
        self.assertEqual(r["gate"], "passed")
        self.assertEqual(r["modules"][0]["cases"], 2)

    def test_failing_command_fails_gate_and_names_it_without_output(self):
        r = self.run_kit([{"name": "ok", "argv": ["{python}", "-I", "ok.py"]},
                          {"name": "regression", "argv": ["{python}", "-I", "bad.py"]}])
        self.assertEqual(r["gate"], "failed")
        self.assertIn("regression", r["modules"][0]["reason"])
        self.assertNotIn("SECRET", str(r))

    def test_undecodable_output_does_not_fail_an_exit_zero_command(self):
        # Copilot on Lydian#6: decoding unused output could turn exit 0 into a failure.
        (self.repo / "bytes.py").write_text("import sys\nsys.stdout.buffer.write(b'\\xff\\xfe')\nsys.stderr.buffer.write(b'\\xff')\n")
        r = self.run_kit([{"name": "bytes", "argv": ["{python}", "-I", "bytes.py"]}])
        self.assertEqual(r["gate"], "passed", r)

    def test_timeout_and_missing_program_name_the_command(self):
        # Copilot on Lydian#6: a timeout or a program that can't start must say which command.
        self.suite["modules"]["repository-commands"]["timeout_seconds"] = 1
        (self.repo / "slow.py").write_text("import time\ntime.sleep(5)\n")
        r = self.run_kit([{"name": "fast", "argv": ["{python}", "-I", "ok.py"]},
                          {"name": "slow suite", "argv": ["{python}", "-I", "slow.py"]}])
        self.assertEqual(r["gate"], "failed")
        self.assertIn("slow suite", r["modules"][0]["reason"])
        self.assertIn("TimeoutExpired", r["modules"][0]["reason"])
        self.assertNotIn("slow.py", r["modules"][0]["reason"])
        self.suite["modules"]["repository-commands"]["allowed_programs"] = ["{python}", "kairos-no-such-program"]
        r = self.run_kit([{"name": "ghost", "argv": ["kairos-no-such-program"]}])
        self.assertIn("ghost", r["modules"][0]["reason"])

    def test_program_outside_central_allowlist_is_refused_before_anything_runs(self):
        marker = self.repo / "ran"
        r = self.run_kit([{"name": "ok", "argv": ["{python}", "-I", "-c", f"open({str(marker)!r}, 'w')"]},
                          {"name": "sh", "argv": ["sh", "-c", "true"]}])
        self.assertEqual(r["gate"], "failed")
        self.assertFalse(marker.exists())

    def test_optional_and_undeclared_is_not_declared_but_cannot_pass_alone(self):
        r = self.run_kit(None)
        self.assertEqual(r["modules"][0]["status"], "not-declared")
        self.assertEqual(r["gate"], "failed")   # nothing actually passed

    def test_optional_undeclared_beside_a_passing_module_passes(self):
        self.suite["modules"]["repository-tests"] = {"allow_skips": False}
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_x.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n")
        adapter = {"schema": 1, "profile": "p", "modules": {"repository-tests": {"directory": "tests"}}}
        r = kit.evaluate(self.repo, adapter, self.suite)
        self.assertEqual(r["gate"], "passed")
        self.assertEqual({m["module"]: m["status"] for m in r["modules"]},
                         {"repository-commands": "not-declared", "repository-tests": "passed"})

    def test_required_when_not_optional(self):
        self.suite["modules"]["repository-commands"]["optional"] = False
        self.assertEqual(self.run_kit(None)["modules"][0]["status"], "blocked")

    def test_malformed_declarations_fail(self):
        for bad in ([], [{"argv": ["{python}", "ok.py"]}], [{"name": "a", "argv": []}],
                    [{"name": "a", "argv": ["{python}", "ok.py"]}, {"name": "a", "argv": ["{python}", "ok.py"]}]):
            with self.subTest(bad=bad):
                self.assertEqual(self.run_kit(bad)["gate"], "failed")

    def test_production_suites_allow_only_python_and_node(self):
        import json
        for name in ("kairos-v1", "kairos-records-v1"):
            policy = json.loads((ROOT / "suites" / f"{name}.json").read_text())["modules"]["repository-commands"]
            self.assertTrue(policy["optional"])
            self.assertEqual(sorted(policy["allowed_programs"]), ["node", "{python}"])


if __name__ == "__main__":
    unittest.main()
