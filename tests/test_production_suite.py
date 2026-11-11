"""Regression guards for the production suite and the shared workflow (Codex review on PR #1)."""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ProductionSuiteTests(unittest.TestCase):
    def test_default_suite_runs_telosmith(self):
        workflow = (ROOT / ".github/workflows/kairos-checks.yml").read_text()
        self.assertIn("default: kairos-v1", workflow)
        suite = json.loads((ROOT / "suites/kairos-v1.json").read_text())
        self.assertEqual(suite["profile"], "kairos-v1")
        self.assertIn("telosmith", suite["modules"])
        self.assertIn("repository-tests", suite["modules"])

    def test_caller_example_uses_production_suite(self):
        self.assertIn("suite: kairos-v1", (ROOT / "examples/caller/kairos.yml").read_text())

    def test_kit_step_fails_when_kit_fails(self):
        # Default Actions bash has no pipefail; `run.py | tee` would hide a failed gate.
        workflow = (ROOT / ".github/workflows/kairos-checks.yml").read_text()
        step = workflow.split("- name: Run the central suite", 1)[1].split("- name:", 1)[0]
        self.assertRegex(step, r"\n\s+shell: bash\b")
        # A duplicated key makes the workflow invalid YAML for Actions.
        self.assertEqual(step.count("shell:"), 1)


    def test_production_example_passes_end_to_end(self):
        import importlib.util, os, shutil, tempfile
        src = os.environ.get("KAIROS_TELOSMITH_SRC", "")
        if not src:
            self.skipTest("set KAIROS_TELOSMITH_SRC")
        spec = importlib.util.spec_from_file_location("kit_run_p", ROOT / "kit/run.py")
        kit = importlib.util.module_from_spec(spec); spec.loader.exec_module(kit)
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            shutil.copytree(ROOT / "examples/consumer_repo", repo)
            adapter = json.loads((repo / ".kairos/adapter.json").read_text())
            suite = json.loads((ROOT / "suites/kairos-v1.json").read_text())
            report = kit.evaluate(repo, adapter, suite)
            self.assertEqual(report["gate"], "passed", report)
            self.assertEqual([m["module"] for m in report["modules"]], ["repository-tests", "telosmith"])


if __name__ == "__main__":
    unittest.main()
