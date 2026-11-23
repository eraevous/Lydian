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


    def test_run_steps_take_inputs_through_env_not_expressions(self):
        # `${{ inputs.x }}` inside `run:` is pasted into the bash script before it runs, so a
        # caller input holding `"; cmd; "` becomes shell code (Actions' "script injection" pattern;
        # Lydian security cycle, 2026-11-19). Inputs reach run steps through env only.
        workflow = (ROOT / ".github/workflows/kairos-checks.yml").read_text()
        for step in workflow.split("- name:")[1:]:
            run = step.split("run: |", 1)[1] if "run: |" in step else ""
            self.assertNotIn("${{", run, "a run block interpolates an expression")

    def test_suite_input_must_name_a_central_suite(self):
        # The suite input picks a file under the kit's suites/; a path ("../../repo/my.json") would
        # let the caller run a suite with its own allowlists. Run the step's guard for real.
        import subprocess
        workflow = (ROOT / ".github/workflows/kairos-checks.yml").read_text()
        step = workflow.split("- name: Run the central suite", 1)[1].split("- name:", 1)[0]
        guard = [line.strip() for line in step.split("run: |", 1)[1].splitlines()
                 if line.strip().startswith("[[") and "KAIROS_SUITE" in line]
        self.assertTrue(guard, "the run step has no suite-name guard")
        script = "set -e\n" + "\n".join(guard) + "\necho ok"
        for name, ok in (("kairos-v1", True), ("kairos-records-v1", True), ("component-v1", True),
                         ("../../repo/mine", False), ("kairos-v1/../x", False), ("", False),
                         ("kairos-v1\nx", False), ("/etc/x", False)):
            run = subprocess.run(["bash", "-c", script], env={"KAIROS_SUITE": name, "PATH": "/usr/bin:/bin"},
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode == 0 and run.stdout.strip() == "ok", ok, (name, run.stderr))


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
            self.assertEqual({m["module"]: m["status"] for m in report["modules"]},
                             {"repository-tests": "passed", "telosmith": "passed",
                              "repository-commands": "not-declared", "hook-guard": "passed"})


HOOK_GUARD_SUITES = ("kairos-v1", "kairos-records-v1")


_ABSENT = object()


def _same_json(a, b):
    # Compare as JSON text, not Python values: True == 1 in Python, but the kit's flag() needs `is True`,
    # and an absent key is not the same as an explicit null (Codex review on #25).
    if a is _ABSENT or b is _ABSENT:
        return a is b
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def hook_guard_differences(first, second):
    """Keys of the `hook-guard` block where two suites differ, with the repository for fingerprints."""
    found = []
    for key in sorted(set(first) | set(second)):
        a, b = first.get(key, _ABSENT), second.get(key, _ABSENT)
        if key == "allowed_hook_commands_sha256" and isinstance(a, dict) and isinstance(b, dict):
            found += [f"{key}[{repo}]" for repo in sorted(set(a) | set(b))
                      if not _same_json(a.get(repo, _ABSENT), b.get(repo, _ABSENT))]
        elif not _same_json(a, b):
            found.append(key)
    return found


class HookGuardSuitesAgree(unittest.TestCase):
    """Both production suites carry their own copy of the hook-guard block (no include mechanism).

    A fingerprint added to one suite but not the other would pass a hook in kairos-v1 repositories
    and fail it in kairos-records-v1 ones. Until the suites share one allowlist file (a format
    change, proposed to Zach in HO-LYD-20261122-WC6J Q2), this keeps the two copies identical.
    """

    def block(self, name):
        return json.loads((ROOT / "suites" / f"{name}.json").read_text())["modules"]["hook-guard"]

    def test_production_suites_have_identical_hook_guard_blocks(self):
        first, second = (self.block(n) for n in HOOK_GUARD_SUITES)
        self.assertEqual(hook_guard_differences(first, second), [],
                         "edit the hook-guard block of both suites/kairos-v1.json and "
                         "suites/kairos-records-v1.json together")

    def test_a_fingerprint_missing_from_one_suite_is_flagged(self):
        first = self.block(HOOK_GUARD_SUITES[0])
        second = json.loads(json.dumps(first))
        second["allowed_hook_commands_sha256"]["*"].pop()
        self.assertEqual(hook_guard_differences(first, second), ["allowed_hook_commands_sha256[*]"])
        del second["allowed_hook_commands_sha256"]["eraevous/MtG"]
        self.assertEqual(hook_guard_differences(first, second),
                         ["allowed_hook_commands_sha256[*]", "allowed_hook_commands_sha256[eraevous/MtG]"])

    def test_other_guard_settings_are_compared_too(self):
        first = self.block(HOOK_GUARD_SUITES[0])
        second = json.loads(json.dumps(first))
        second["forbidden_root_modules"].remove("sitecustomize")
        second["run_first"] = False
        self.assertEqual(hook_guard_differences(first, second), ["forbidden_root_modules", "run_first"])

    def test_json_type_and_key_presence_differences_are_flagged(self):
        # Python equality would miss each of these (Codex review on #25).
        first = self.block(HOOK_GUARD_SUITES[0])
        second = json.loads(json.dumps(first))
        second["run_first"] = 1
        second["adapter_optional"] = None
        del first["adapter_optional"]
        second["allowed_hook_commands_sha256"]["eraevous/Example"] = None
        self.assertEqual(hook_guard_differences(first, second),
                         ["adapter_optional", "allowed_hook_commands_sha256[eraevous/Example]", "run_first"])

    def test_identical_copy_is_not_flagged(self):
        # Clean decoy: a separate but equal copy (what the two suite files hold today) passes.
        first = self.block(HOOK_GUARD_SUITES[0])
        self.assertEqual(hook_guard_differences(first, json.loads(json.dumps(first))), [])


if __name__ == "__main__":
    unittest.main()
