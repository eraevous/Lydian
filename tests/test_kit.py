import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kit_run", ROOT / "kit/run.py")
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)


class KitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        shutil.copytree(ROOT / "examples/component_repo", self.repo)
        self.adapter = kit.read_json(self.repo / "adapter.json")
        self.suite = kit.read_json(ROOT / "suites/component-v1.json")

    def run_kit(self):
        return kit.evaluate(self.repo, self.adapter, self.suite)

    def only(self, name):
        self.suite["modules"] = {name: self.suite["modules"][name]}

    def test_clean_decoy_all_modules_pass(self):
        report = self.run_kit()
        self.assertEqual(report["gate"], "passed")
        self.assertEqual([r["cases"] for r in report["modules"]], [5, 1, 2])

    def test_wire_wrong_version_fails(self):
        self.only("interoperability")
        path = self.repo / "adapter.py"
        path.write_text(path.read_text().replace('"kit-demo@1"', '"kit-demo@2"'))
        self.assertEqual(self.run_kit()["gate"], "failed")

    def test_wire_boolean_is_not_number(self):
        self.only("interoperability")
        self.suite["modules"]["interoperability"]["cases"][0]["wire"]["payload"]["enabled"] = 0
        self.assertEqual(self.run_kit()["gate"], "failed")

    def test_consumer_crash_is_not_validation(self):
        self.only("interoperability")
        path = self.repo / "adapter.py"
        path.write_text(path.read_text().replace("sys.exit(2)", "sys.exit(1)"))
        report = self.run_kit()
        self.assertEqual(report["gate"], "failed")
        self.assertIn("explicitly reject", report["modules"][0]["reason"])

    def test_roundtrip_agreement_does_not_override_golden_wire(self):
        self.only("interoperability")
        path = self.repo / "adapter.py"
        path.write_text(path.read_text().replace('"payload"', '"data"'))
        self.assertEqual(self.run_kit()["gate"], "failed")

    def test_contract_without_negative_fixture_fails(self):
        self.only("interoperability")
        self.suite["modules"]["interoperability"]["cases"][0]["invalid_wire"] = []
        self.assertEqual(self.run_kit()["gate"], "failed")

    def test_import_violation_fails(self):
        self.only("modularity")
        with (self.repo / "adapter.py").open("a") as stream:
            stream.write("\nimport requests.sessions\n")
        self.assertEqual(self.run_kit()["gate"], "failed")

    def test_from_import_cannot_bypass_submodule_boundary(self):
        self.only("modularity")
        self.adapter["modules"]["modularity"]["boundaries"][0]["forbidden_imports"] = ["requests.sessions"]
        with (self.repo / "adapter.py").open("a") as stream:
            stream.write("\nfrom requests import sessions\n")
        self.assertEqual(self.run_kit()["gate"], "failed")

    def test_string_and_nearby_module_are_clean_decoys(self):
        self.only("modularity")
        with (self.repo / "adapter.py").open("a") as stream:
            stream.write('\nexample = "import requests"\nimport requests_extra\n')
        self.assertEqual(self.run_kit()["gate"], "passed")

    def test_empty_boundary_match_fails(self):
        self.only("modularity")
        self.adapter["modules"]["modularity"]["boundaries"][0]["files"] = "absent/*.py"
        self.assertEqual(self.run_kit()["gate"], "failed")

    def test_boundary_outside_repo_fails(self):
        self.only("modularity")
        self.adapter["modules"]["modularity"]["boundaries"][0]["files"] = "../*.py"
        self.assertEqual(self.run_kit()["gate"], "failed")

    def test_syntax_error_fails(self):
        self.only("modularity")
        (self.repo / "adapter.py").write_text("def broken(:\n")
        self.assertEqual(self.run_kit()["gate"], "failed")

    def _skip_with(self, reason):
        self.only("repository-tests")
        (self.repo / "test_skip_extra.py").write_text(
            "import unittest\n\nclass S(unittest.TestCase):\n"
            f"    @unittest.skip({reason!r})\n    def test_s(self):\n        pass\n")

    def test_allowlisted_skip_reason_passes(self):
        self._skip_with("specify CLI not installed")
        self.suite["modules"]["repository-tests"]["allowed_skip_reasons"] = ["specify CLI not installed"]
        self.assertEqual(self.run_kit()["gate"], "passed")

    def test_unlisted_skip_reason_fails(self):
        self._skip_with("network unavailable")
        self.suite["modules"]["repository-tests"]["allowed_skip_reasons"] = ["specify CLI not installed"]
        self.assertEqual(self.run_kit()["gate"], "failed")

    def test_adapter_cannot_add_skip_reasons(self):
        self._skip_with("anything")
        self.adapter["modules"]["repository-tests"]["allowed_skip_reasons"] = ["anything"]
        self.assertEqual(self.run_kit()["gate"], "failed")

    def test_zero_unit_tests_fails(self):
        self.only("repository-tests")
        (self.repo / "test_adapter.py").unlink()
        self.assertEqual(self.run_kit()["gate"], "failed")

    def test_all_unit_tests_skipped_fails_even_when_skips_allowed(self):
        self.only("repository-tests")
        self.suite["modules"]["repository-tests"]["allow_skips"] = True
        path = self.repo / "test_adapter.py"
        path.write_text(path.read_text().replace("class DemoTests", '@unittest.skip("fixture")\nclass DemoTests'))
        self.assertEqual(self.run_kit()["gate"], "failed")

    def test_real_unit_failure_fails(self):
        self.only("repository-tests")
        path = self.repo / "test_adapter.py"
        path.write_text(path.read_text().replace("self.assertEqual", "self.assertNotEqual"))
        self.assertEqual(self.run_kit()["gate"], "failed")

    def test_unit_stdout_does_not_corrupt_result(self):
        self.only("repository-tests")
        path = self.repo / "test_adapter.py"
        path.write_text(path.read_text().replace("import unittest", 'import unittest\nprint("a harmless test log")'))
        self.assertEqual(self.run_kit()["gate"], "passed")

    def test_unit_child_process_stdout_does_not_corrupt_result(self):
        self.only("repository-tests")
        path = self.repo / "test_adapter.py"
        path.write_text(path.read_text().replace("import unittest", '''import unittest, subprocess, sys
subprocess.run([sys.executable, '-c', 'print(123)'], check=True)'''))
        self.assertEqual(self.run_kit()["gate"], "passed")

    def test_skipped_subtests_never_produce_negative_case_count(self):
        self.only("repository-tests")
        self.suite["modules"]["repository-tests"]["allow_skips"] = True
        (self.repo / "test_adapter.py").write_text('''import unittest
class Example(unittest.TestCase):
    def test_subtests(self):
        for i in range(3):
            with self.subTest(i=i):
                self.skipTest("fixture")
''')
        report = self.run_kit()
        self.assertEqual(report["gate"], "failed")
        self.assertEqual(report["modules"][0]["cases"], 0)

    def test_explicit_skip_policy_can_allow_skip_beside_completed_test(self):
        self.only("repository-tests")
        self.suite["modules"]["repository-tests"]["allow_skips"] = True
        with (self.repo / "test_adapter.py").open("a") as stream:
            stream.write('''
class Partial(unittest.TestCase):
    def test_subtests(self):
        with self.subTest(i=1):
            self.skipTest("fixture")
''')
        report = self.run_kit()
        self.assertEqual(report["gate"], "passed")
        self.assertEqual(report["modules"][0]["cases"], 2)

    def test_malformed_json_shapes_are_controlled_failures(self):
        for adapter, suite in [(None, self.suite), ([], self.suite), (self.adapter, []),
                               ({**self.adapter, "modules": []}, self.suite)]:
            with self.subTest(adapter=adapter, suite=suite):
                with self.assertRaises(kit.KitError):
                    kit.evaluate(self.repo, adapter, suite)

    def test_missing_required_module_blocks_gate(self):
        del self.adapter["modules"]["interoperability"]
        report = self.run_kit()
        self.assertEqual(report["gate"], "failed")
        self.assertEqual(report["modules"][0]["status"], "blocked")

    def test_unknown_module_blocks_gate(self):
        self.suite["modules"]["future"] = {}
        self.assertEqual(self.run_kit()["gate"], "failed")

    def test_empty_suite_is_not_green(self):
        self.suite["modules"] = {}
        with self.assertRaises(kit.KitError):
            self.run_kit()

    def test_central_test_edit_applies_to_two_unchanged_adapters(self):
        second = Path(self.temp.name) / "second"
        shutil.copytree(self.repo, second)
        adapters_before = [(repo / "adapter.json").read_bytes() for repo in [self.repo, second]]
        suite_before = copy.deepcopy(self.suite)
        for repo in [self.repo, second]:
            self.assertEqual(kit.evaluate(repo, self.adapter, self.suite)["gate"], "passed")
        # A centrally added incompatible wire fixture now fails both consumers.
        case = copy.deepcopy(self.suite["modules"]["interoperability"]["cases"][0])
        case["wire"]["schema"] = "kit-demo@2"
        self.suite["modules"]["interoperability"]["cases"].append(case)
        for repo in [self.repo, second]:
            self.assertEqual(kit.evaluate(repo, self.adapter, self.suite)["gate"], "failed")
        self.suite = suite_before
        for repo, before in zip([self.repo, second], adapters_before):
            self.assertEqual(kit.evaluate(repo, self.adapter, self.suite)["gate"], "passed")
            self.assertEqual((repo / "adapter.json").read_bytes(), before)

    def test_command_timeout_is_failure(self):
        with self.assertRaises(kit.KitError):
            kit.command(self.repo, ["{python}", "-I", "-c", "import time; time.sleep(2)"], timeout=.05)

    def test_cli_emits_json_and_failure_exit(self):
        self.suite["modules"]["future"] = {}
        suite_path = Path(self.temp.name) / "suite.json"
        suite_path.write_text(json.dumps(self.suite))
        run = subprocess.run([sys.executable, "-I", str(ROOT / "kit/run.py"), "--repo", str(self.repo),
                              "--adapter", str(self.repo / "adapter.json"), "--suite", str(suite_path)],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(json.loads(run.stdout)["gate"], "failed")

    def test_cli_malformed_adapter_still_reports_json(self):
        adapter_path = Path(self.temp.name) / "bad-adapter.json"
        adapter_path.write_text("[]")
        run = subprocess.run([sys.executable, "-I", str(ROOT / "kit/run.py"), "--repo", str(self.repo),
                              "--adapter", str(adapter_path), "--suite", str(ROOT / "suites/component-v1.json")],
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(json.loads(run.stdout)["gate"], "failed")
        self.assertNotIn("Traceback", run.stderr)


if __name__ == "__main__":
    unittest.main()
