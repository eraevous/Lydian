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


if __name__ == "__main__":
    unittest.main()
