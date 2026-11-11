"""Telosmith module: the kit, not each repo, runs the records check (violation + clean decoy)."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kit_run_t", ROOT / "kit/run.py")
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)
SRC = os.environ.get("KAIROS_TELOSMITH_SRC", "")


@unittest.skipUnless(SRC and (Path(SRC) / "aec").is_dir(), "set KAIROS_TELOSMITH_SRC to a Telosmith src/ checkout")
class TelosmithModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        shutil.copytree(ROOT / "examples/component_repo", self.repo)
        (self.repo / ".aec").mkdir()
        record = {"id": "P0", "version": 1, "type": "purpose", "text": "Demo component for the shared kit.",
                  "epistemic": "provisional"}
        (self.repo / ".aec/records.jsonl").write_text(json.dumps(record) + "\n")
        self.adapter = {"schema": 1, "profile": "t", "modules": {"telosmith": {}}}
        self.suite = {"schema": 1, "profile": "t", "modules": {"telosmith": {}}}

    def gate(self):
        return kit.evaluate(self.repo, self.adapter, self.suite)

    def test_clean_records_pass(self):
        report = self.gate()
        self.assertEqual(report["gate"], "passed", report)

    def test_malformed_record_fails(self):
        with (self.repo / ".aec/records.jsonl").open("a") as stream:
            stream.write("{not json\n")
        self.assertEqual(self.gate()["gate"], "failed")

    def test_missing_records_fails(self):
        (self.repo / ".aec/records.jsonl").unlink()
        report = self.gate()
        self.assertEqual(report["gate"], "failed")
        self.assertIn("no Telosmith records", report["modules"][0]["reason"])

    def test_missing_telosmith_source_fails_closed(self):
        old = os.environ.pop("KAIROS_TELOSMITH_SRC")
        self.addCleanup(os.environ.__setitem__, "KAIROS_TELOSMITH_SRC", old)
        self.assertEqual(self.gate()["gate"], "failed")


if __name__ == "__main__":
    unittest.main()
