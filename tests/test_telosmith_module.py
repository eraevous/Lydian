"""Telosmith module: the kit, not each repo, runs the records check (violation + clean decoy)."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
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

    def test_empty_or_comment_only_records_fail(self):
        (self.repo / ".aec/records.jsonl").write_text("// header comment only\n\n")
        report = self.gate()
        self.assertEqual(report["gate"], "failed")
        self.assertIn("no records", report["modules"][0]["reason"])

    def test_missing_telosmith_source_fails_closed(self):
        old = os.environ.pop("KAIROS_TELOSMITH_SRC")
        self.addCleanup(os.environ.__setitem__, "KAIROS_TELOSMITH_SRC", old)
        self.assertEqual(self.gate()["gate"], "failed")


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                          cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@unittest.skipUnless(SRC and (Path(SRC) / "aec").is_dir(), "set KAIROS_TELOSMITH_SRC to a Telosmith src/ checkout")
class CodeImpactTests(unittest.TestCase):
    """Report-only code-impact against the PR base, as aec.yml ran it (DEC-256), now from the kit."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        shutil.copytree(ROOT / "examples/component_repo", self.repo)
        (self.repo / ".aec").mkdir()
        records = [{"id": "P0", "version": 1, "type": "purpose", "text": "Demo component for the shared kit.",
                    "epistemic": "provisional"},
                   {"id": "R1", "version": 1, "type": "requirement", "text": "consume rejects a bad wire.",
                    "epistemic": "provisional", "relations": [{"rel": "implemented_by", "to": "code:adapter.consume"}]}]
        (self.repo / ".aec/records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.out = Path(self.temp.name) / "code-impact.md"
        self.adapter = {"schema": 1, "profile": "t", "modules": {"telosmith": {}}}
        self.suite = {"schema": 1, "profile": "t", "modules": {"telosmith": {}}}
        for key in ("KAIROS_BASE_SHA", "KAIROS_CODE_IMPACT_OUT"):
            old = os.environ.pop(key, None)
            if old is not None:
                self.addCleanup(os.environ.__setitem__, key, old)
            else:
                self.addCleanup(os.environ.pop, key, None)
        os.environ["KAIROS_CODE_IMPACT_OUT"] = str(self.out)

    def change_linked_code(self):
        adapter = self.repo / "adapter.py"
        adapter.write_text(adapter.read_text().replace("invalid demo wire", "bad demo wire"))
        git(self.repo, "commit", "-qam", "change")

    def gate(self):
        return kit.evaluate(self.repo, self.adapter, self.suite)

    def test_change_to_linked_code_is_reported(self):
        self.change_linked_code()
        os.environ["KAIROS_BASE_SHA"] = self.base
        report = self.gate()
        self.assertEqual(report["gate"], "passed", report)
        text = self.out.read_text()
        self.assertIn(f"Telosmith code-impact (structural, report-only) vs {self.base}", text)
        self.assertIn("revise code:adapter.consume", text)

    def test_unlinked_change_reports_no_events(self):
        # Decoy: a change no record links to produces a report, but no change event.
        (self.repo / "notes.py").write_text("X = 1\n")
        git(self.repo, "add", "notes.py")
        git(self.repo, "commit", "-qm", "unlinked")
        os.environ["KAIROS_BASE_SHA"] = self.base
        self.assertEqual(self.gate()["gate"], "passed")
        text = self.out.read_text()
        self.assertIn("code-impact (structural, report-only)", text)
        self.assertNotIn("code:adapter.consume", text)

    def test_no_base_is_skipped_cleanly(self):
        self.change_linked_code()
        self.assertEqual(self.gate()["gate"], "passed")
        self.assertIn("code-impact skipped: no base revision for this event", self.out.read_text())

    def test_unknown_or_option_shaped_base_is_skipped(self):
        self.change_linked_code()
        for base in ("0" * 40, "--output=/tmp/x", "HEAD~1"):
            with self.subTest(base=base):
                os.environ["KAIROS_BASE_SHA"] = base
                self.assertEqual(self.gate()["gate"], "passed")
                self.assertIn("code-impact skipped", self.out.read_text())

    def test_report_only_never_turns_a_failing_check_green_or_a_passing_one_red(self):
        self.change_linked_code()
        os.environ["KAIROS_BASE_SHA"] = self.base
        with (self.repo / ".aec/records.jsonl").open("a") as stream:
            stream.write("{not json\n")
        self.assertEqual(self.gate()["gate"], "failed")
        self.assertFalse(self.out.exists(), "code-impact runs only after the records check passes, as in aec.yml")

    def test_without_an_output_path_nothing_is_written(self):
        del os.environ["KAIROS_CODE_IMPACT_OUT"]
        os.environ["KAIROS_BASE_SHA"] = self.base
        self.assertEqual(self.gate()["gate"], "passed")
        self.assertFalse(self.out.exists())

    def test_unwritable_output_path_does_not_fail_the_gate(self):
        os.environ["KAIROS_CODE_IMPACT_OUT"] = str(Path(self.temp.name) / "missing-dir" / "code-impact.md")
        os.environ["KAIROS_BASE_SHA"] = self.base
        self.assertEqual(self.gate()["gate"], "passed")

    def patch_code_impact_call(self, fake):
        real = kit.command
        def command(root, argv, *args, **kwargs):
            if "code-impact" in argv:
                return fake(real, root, argv, *args, **kwargs)
            return real(root, argv, *args, **kwargs)
        kit.command = command
        self.addCleanup(setattr, kit, "command", real)

    def test_failed_code_impact_run_is_skipped_not_reported(self):
        # Without --exit-code a nonzero exit is an execution error, not detected impact (Copilot, #18).
        self.change_linked_code()
        os.environ["KAIROS_BASE_SHA"] = self.base
        def fail(real, root, argv, *args, **kwargs):
            return real(root, argv[:-3] + ["code-impact", "/nonexistent/records.jsonl", argv[-2], argv[-1]], *args, **kwargs)
        self.patch_code_impact_call(fail)
        self.assertEqual(self.gate()["gate"], "passed")
        text = self.out.read_text()
        self.assertIn("code-impact skipped: Telosmith code-impact exited", text)
        self.assertNotIn("## Telosmith code-impact", text)

    def test_undecodable_code_impact_output_does_not_fail_the_gate(self):
        # A record with an escaped lone surrogate can make output that isn't UTF-8 (Copilot, #18).
        self.change_linked_code()
        os.environ["KAIROS_BASE_SHA"] = self.base
        def undecodable(real, root, argv, *args, **kwargs):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        self.patch_code_impact_call(undecodable)
        self.assertEqual(self.gate()["gate"], "passed")
        self.assertIn("code-impact skipped: output was not valid UTF-8", self.out.read_text())


if __name__ == "__main__":
    unittest.main()
