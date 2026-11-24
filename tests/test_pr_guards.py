"""test-tampering and new-dependencies: each failure case it must flag, and clean decoys it must not.

Handoff HO-LYD-20261124-SGZN (survey issues #1 and #5). Every case builds a real git repository with
a base commit and a head commit; registry lookups are stubbed (no network in tests).
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kit_run_pr", ROOT / "kit/run.py")
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)

PY_TEST = """import unittest
import pytest

class T(unittest.TestCase):
    def test_a(self):
        self.assertEqual(1, 1)
        self.assertTrue(True)

def test_b():
    assert 1 == 1
"""
JS_TEST = """import { expect, test } from "bun:test";
test("a", () => { expect(1).toBe(1); expect(2).toBe(2); });
"""


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


class Base(unittest.TestCase):
    module = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        self.env = {"KAIROS_EVENT_NAME": "pull_request", "KAIROS_PR_BODY": "Ordinary change.",
                    "KAIROS_PR_AUTHOR": "eraevous", "KAIROS_PR_CREATED_AT": "2026-12-01T00:00:00Z"}
        self.policy = {"enforce_from": "2026-11-26T00:00:00Z"}

    def write(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, message="c"):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.repo, "rev-parse", "HEAD").strip()

    def base(self):
        self.env["KAIROS_BASE_SHA"] = self.commit("base")

    def run_module(self, **env):
        self.commit("head")
        with mock.patch.dict(os.environ, {**self.env, **env}, clear=False):
            return kit.MODULES[self.module](self.repo.resolve(), {}, self.policy)

    def assert_flags(self, *fragments, **env):
        with self.assertRaises(kit.KitError) as caught:
            self.run_module(**env)
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))
        return str(caught.exception)


class TestTamperingTests(Base):
    module = "test-tampering"

    def setUp(self):
        super().setUp()
        self.write("tests/test_x.py", PY_TEST)
        self.write("src/app.test.ts", JS_TEST)
        self.write("README.md", "hi\n")
        self.base()

    # Cases it must flag.
    def test_deleted_test_file(self):
        (self.repo / "tests/test_x.py").unlink()
        self.assert_flags("tests/test_x.py", "deleted")

    def test_renamed_away_from_a_test_name(self):
        git(self.repo, "mv", "tests/test_x.py", "tests/helpers_x.py")
        self.assert_flags("tests/test_x.py", "renamed to non-test tests/helpers_x.py")

    def test_fewer_python_assertions(self):
        self.write("tests/test_x.py", PY_TEST.replace("        self.assertTrue(True)\n", ""))
        self.assert_flags("tests/test_x.py", "assertions 3 -> 2")

    def test_fewer_js_expects(self):
        self.write("src/app.test.ts", JS_TEST.replace(" expect(2).toBe(2);", ""))
        self.assert_flags("src/app.test.ts", "assertions 2 -> 1")

    def test_new_pytest_skip(self):
        self.write("tests/test_x.py", PY_TEST.replace("def test_b", "@pytest.mark.skip\ndef test_b"))
        self.assert_flags("tests/test_x.py", "skips 0 -> 1")

    def test_new_unittest_skip_and_xfail(self):
        text = PY_TEST.replace("    def test_a", "    @unittest.skip('x')\n    def test_a")
        self.write("tests/test_x.py", text.replace("def test_b", "@pytest.mark.xfail\ndef test_b"))
        self.assert_flags("skips 0 -> 2")

    def test_new_js_skips(self):
        self.write("src/app.test.ts", JS_TEST + 'test.skip("b", () => {});\nxit("c", () => {});\ntest.todo("d");\n')
        self.assert_flags("src/app.test.ts", "skips 0 -> 3")

    def test_every_finding_is_named(self):
        (self.repo / "tests/test_x.py").unlink()
        self.write("src/app.test.ts", JS_TEST.replace(" expect(2).toBe(2);", ""))
        message = self.assert_flags("tests/test_x.py", "src/app.test.ts")
        self.assertIn("DEC-", message)  # says how to clear it

    def test_body_line_without_a_reason_does_not_clear(self):
        (self.repo / "tests/test_x.py").unlink()
        self.assert_flags("deleted", KAIROS_PR_BODY="Tests-changed-because:   \n")

    def test_decision_id_must_be_a_whole_token(self):
        (self.repo / "tests/test_x.py").unlink()
        self.assert_flags("deleted", KAIROS_PR_BODY="see XDEC-12 and DEC-")

    def test_module_level_and_imported_mark_skips(self):
        self.write("tests/test_x.py", "from pytest import mark\npytestmark = pytest.mark.skip\n" + PY_TEST.replace("def test_b", "@mark.xfail\ndef test_b"))
        self.assert_flags("skips 0 -> 2")

    def test_focused_js_test_counts_as_a_skip(self):
        self.write("src/app.test.ts", JS_TEST.replace('test("a"', 'test.only("a"') + 'test.skipIf(true)("b", () => {});\n')
        self.assert_flags("skips 0 -> 2")

    def test_assertions_in_comments_do_not_count(self):
        self.write("tests/test_x.py", PY_TEST.replace("assert 1 == 1", "assert True  # assert 1 == 1").replace("        self.assertTrue(True)\n", ""))
        self.assert_flags("assertions 3 -> 2")

    def test_new_test_file_with_a_focused_test(self):
        self.write("src/empty.test.js", 'test.only("nothing", () => {});\n')
        self.assert_flags("src/empty.test.js", "new test file with skips 0 -> 1")

    def test_block_comment_and_string_disable_assertions(self):
        self.write("src/app.test.ts", JS_TEST.replace("expect(2).toBe(2);", "/* expect(2).toBe(2); */"))
        self.write("tests/test_x.py", PY_TEST.replace("    assert 1 == 1", '    """assert 1 == 1"""'))
        self.assert_flags("src/app.test.ts: assertions 2 -> 1", "tests/test_x.py: assertions 3 -> 2")

    def test_comment_delimiters_in_strings_hide_nothing(self):
        self.write("src/focus.test.js", 'const start = "/*";\ntest.only("focused", () => {});\nconst end = "*/";\n')
        self.write("tests/test_x.py", PY_TEST.replace("    assert 1 == 1", '    x = "#"; assert 1 == 1').replace("        self.assertTrue(True)\n", ""))
        self.assert_flags("src/focus.test.js: new test file with skips 0 -> 1", "tests/test_x.py: assertions 3 -> 2")

    def test_interpolated_code_is_scanned(self):
        self.write("src/tpl.test.js", 'const r = `${test.only("focused", () => {})}`;\n')
        self.write("tests/test_f.py", 'x = f"{pytest.skip()}"\ny = f"{{literal}}"\n')
        self.assert_flags("src/tpl.test.js: new test file with skips 0 -> 1", "tests/test_f.py: new test file with skips 0 -> 1")

    def test_file_renamed_into_a_test_name_is_checked(self):
        self.write("src/dormant.js", 'test.only("focused", () => {});\n')
        self.commit("dormant")
        self.env["KAIROS_BASE_SHA"] = git(self.repo, "rev-parse", "HEAD").strip()
        git(self.repo, "mv", "src/dormant.js", "src/active.test.js")
        self.assert_flags("src/active.test.js: renamed into a test name with skips 0 -> 1")

    def test_plain_template_and_fstring_text_is_not_code(self):
        counts = kit.tampering_counts("a.test.js", "const s = `expect( ${1 + 1} test.only(`;\n")
        self.assertEqual(counts, (0, 0))
        self.assertEqual(kit.tampering_counts("t.py", 'x = f"assert {y} pytest.skip("\n'), (0, 0))

    def test_symlinked_test_file_fails(self):
        self.write("src/payload.js", 'test.only("focused", () => {});\n')
        (self.repo / "src/focus.test.js").symlink_to("payload.js")
        self.assert_flags("src/focus.test.js: a symlink")

    def test_chained_each_after_focus_or_skip(self):
        self.write("src/each.test.js", 'test.only.each([[1]])("focused", () => {});\ntest.skip.each([[2]])("s", () => {});\nxit.each([[3]])("x", () => {});\n')
        self.assert_flags("src/each.test.js: new test file with skips 0 -> 3")

    # Clean decoys it must not flag.
    def test_untouched_tests_pass(self):
        self.write("README.md", "changed\n")
        self.assertEqual(self.run_module()["cases"], 2)

    def test_added_assertions_and_new_test_files_pass(self):
        self.write("tests/test_x.py", PY_TEST + "\ndef test_c():\n    assert True\n")
        self.write("tests/test_new.py", "def test_n():\n    assert 1\n")
        self.assertEqual(self.run_module()["cases"], 3)

    def test_rename_between_test_names_keeping_counts_passes(self):
        git(self.repo, "mv", "tests/test_x.py", "tests/test_y.py")
        self.run_module()

    def test_mentions_in_names_are_not_counted(self):
        # assertEqual is one assertion, not also an `assert`; a helper named assert_ok is not one.
        counts = kit.tampering_counts("t.py", "self.assertEqual(a, b)\nmy_assert_ok(x)\n")
        self.assertEqual(counts, (1, 0))

    def test_decision_id_clears(self):
        (self.repo / "tests/test_x.py").unlink()
        out = self.run_module(KAIROS_PR_BODY="Removes the old suite (DEC-20261124-AB3K).")
        self.assertIn("cleared by the PR body", out["note"])

    def test_sequential_decision_id_clears(self):
        (self.repo / "tests/test_x.py").unlink()
        self.run_module(KAIROS_PR_BODY="Per DEC-255.")

    def test_tests_changed_because_clears(self):
        self.write("tests/test_x.py", PY_TEST.replace("        self.assertTrue(True)\n", ""))
        self.run_module(KAIROS_PR_BODY="Intro\nTests-changed-because: duplicate assertion\n")

    def test_dependabot_is_ignored(self):
        (self.repo / "tests/test_x.py").unlink()
        self.assertIn("dependabot", self.run_module(KAIROS_PR_AUTHOR="dependabot[bot]")["note"])

    def test_not_a_pull_request_is_not_checked(self):
        (self.repo / "tests/test_x.py").unlink()
        out = self.run_module(KAIROS_EVENT_NAME="push")
        self.assertEqual(out["cases"], 0)
        self.assertIn("not a pull request", out["note"])

    def test_pr_opened_before_enforcement_only_warns(self):
        (self.repo / "tests/test_x.py").unlink()
        out = self.run_module(KAIROS_PR_CREATED_AT="2026-11-25T12:00:00Z")
        self.assertTrue(any("tests/test_x.py" in w and "deleted" in w for w in out["warnings"]))

    def test_missing_base_fails_closed_on_a_pull_request(self):
        with self.assertRaises(kit.KitError):
            self.run_module(KAIROS_BASE_SHA="")


class NewDependencyTests(Base):
    module = "new-dependencies"

    def setUp(self):
        super().setUp()
        self.write("requirements.txt", "requests==2.31\n# comment\n")
        self.write("package.json", json.dumps({"dependencies": {"react": "^18.0.0"}}))
        self.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["httpx>=0.27"]\n')
        self.base()
        self.calls = []

        def fake(url):
            self.calls.append(url)
            return 404 if "invented" in url else 200
        patcher = mock.patch.object(kit, "registry_status", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    # Cases it must flag.
    def test_new_requirement_without_decision(self):
        self.write("requirements.txt", "requests==2.31\nrich>=13\n")
        self.assert_flags("pypi:rich", "no decision id")

    def test_new_package_json_dependency_without_decision(self):
        self.write("package.json", json.dumps({"dependencies": {"react": "^18.0.0"},
                                               "devDependencies": {"@types/node": "^20"}}))
        self.assert_flags("npm:@types/node", "no decision id")

    def test_new_pyproject_dependency_and_optional_group(self):
        self.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["httpx>=0.27", "Click[extra]>=8"]\n'
                   '[project.optional-dependencies]\ndev = ["pytest"]\n')
        self.assert_flags("pypi:click", "pypi:pytest")

    def test_invented_package_fails_even_with_decision(self):
        self.write("requirements.txt", "requests==2.31\ninvented-pkg-zz\n")
        self.assert_flags("pypi:invented-pkg-zz", "package does not exist (possibly invented)",
                          KAIROS_PR_BODY="DEC-20261124-AB3K")

    def test_new_manifest_in_a_subfolder(self):
        self.write("dashboard/package.json", json.dumps({"dependencies": {"vite": "5"}}))
        self.assert_flags("npm:vite")

    def test_symlinked_manifest_fails(self):
        (self.repo / "requirements.txt").unlink()
        self.write("requests", "invented-pkg-zz\n")
        (self.repo / "requirements.txt").symlink_to("requests")
        self.assert_flags("requirements.txt: a symlink")

    def test_local_to_registry_switch_is_new(self):
        self.write("package.json", json.dumps({"dependencies": {"react": "^18.0.0", "helper": "file:./helper"}}))
        self.write("requirements.txt", "requests==2.31\nmylib @ file:///src/mylib\n")
        self.env["KAIROS_BASE_SHA"] = self.commit("local deps")
        self.write("package.json", json.dumps({"dependencies": {"react": "^18.0.0", "helper": "^1.0.0"}}))
        self.write("requirements.txt", "requests==2.31\nmylib==1.0\n")
        self.assert_flags("npm:helper", "pypi:mylib", "no decision id")

    def test_local_declaration_in_another_group_does_not_mask_a_registry_one(self):
        self.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["httpx>=0.27", "invented-pkg-zz"]\n'
                   '[project.optional-dependencies]\ndev = ["invented-pkg-zz @ file:///src/local"]\n')
        self.assert_flags("pypi:invented-pkg-zz", "possibly invented", KAIROS_PR_BODY="DEC-1")

    def test_build_system_requires_are_checked(self):
        self.write("pyproject.toml", '[build-system]\nrequires = ["invented-build-zz"]\n'
                   '[project]\nname = "x"\ndependencies = ["httpx>=0.27"]\n')
        self.assert_flags("pypi:invented-build-zz", "no decision id")

    def test_uv_local_source_is_not_looked_up_but_needs_a_decision(self):
        self.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["httpx>=0.27", "invented-local-zz"]\n'
                   '[tool.uv.sources]\ninvented-local-zz = { path = "libs/local" }\n')
        self.assert_flags("pypi:invented-local-zz", "no decision id")
        self.calls.clear()
        self.run_module(KAIROS_PR_BODY="DEC-1")
        self.assertEqual(self.calls, [])

    def test_uv_index_source_is_still_looked_up(self):
        self.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["httpx>=0.27", "invented-pkg-zz"]\n'
                   '[tool.uv.sources]\ninvented-pkg-zz = { index = "pypi" }\n')
        self.assert_flags("possibly invented", KAIROS_PR_BODY="DEC-1")

    def test_npm_alias_target_is_looked_up(self):
        self.write("package.json", json.dumps({"dependencies": {"react": "^18.0.0", "helper": "npm:invented-pkg-zz@1",
                                                                 "scoped": "npm:@scope/pkg@2"}}))
        self.assert_flags("npm:invented-pkg-zz", "possibly invented", KAIROS_PR_BODY="DEC-1")
        self.assertIn("https://registry.npmjs.org/@scope%2Fpkg", self.calls)

    def test_scoped_npm_name_is_quoted_in_the_url(self):
        self.write("package.json", json.dumps({"dependencies": {"react": "^18.0.0", "@scope/pkg": "1"}}))
        self.run_module(KAIROS_PR_BODY="DEC-1")
        self.assertEqual(self.calls, ["https://registry.npmjs.org/@scope%2Fpkg"])

    # Clean decoys it must not flag.
    def test_a_stray_manifest_name_does_not_exempt_a_package(self):
        self.write("fixtures/package.json", json.dumps({"name": "invented-pkg-zz"}))
        self.write("package.json", json.dumps({"dependencies": {"react": "^18.0.0", "invented-pkg-zz": "1"}}))
        self.assert_flags("npm:invented-pkg-zz", "possibly invented", KAIROS_PR_BODY="DEC-1")

    def test_private_registry_404_is_unconfirmed_not_invented(self):
        self.write(".npmrc", "@corp:registry=https://npm.corp.example/\n")
        self.write("package.json", json.dumps({"dependencies": {"react": "^18.0.0", "@corp/invented-x": "1"}}))
        out = self.run_module(KAIROS_PR_BODY="DEC-1")
        self.assertTrue(any("private" in w for w in out["warnings"]))

    def test_public_registry_configured_explicitly_exempts_nothing(self):
        self.write(".npmrc", "registry=https://registry.npmjs.org/\n")
        self.write("requirements.txt", "--index-url https://pypi.org/simple\nrequests==2.31\ninvented-pkg-zz\n")
        self.write("package.json", json.dumps({"dependencies": {"react": "^18.0.0", "invented-npm-zz": "1"}}))
        self.assert_flags("npm:invented-npm-zz", "pypi:invented-pkg-zz", "possibly invented", KAIROS_PR_BODY="DEC-1")

    def test_private_uv_index_in_pyproject_is_unconfirmed(self):
        self.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["httpx>=0.27", "invented-pkg-zz"]\n'
                   '[[tool.uv.index]]\nname = "corp"\nurl = "https://pypi.corp.example/simple"\n')
        out = self.run_module(KAIROS_PR_BODY="DEC-1")
        self.assertTrue(any("private" in w for w in out["warnings"]))

    def test_private_python_index_404_is_unconfirmed(self):
        self.write("requirements.txt", "--extra-index-url https://pypi.corp.example/simple\nrequests==2.31\ninvented-pkg-zz\n")
        out = self.run_module(KAIROS_PR_BODY="DEC-1")
        self.assertTrue(any("private" in w for w in out["warnings"]))

    def test_self_extra_and_workspace_sibling_are_not_looked_up(self):
        self.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["httpx>=0.27"]\n'
                   '[project.optional-dependencies]\nall = ["x[a]"]\n')
        self.write("libs/my-lib/package.json", json.dumps({"name": "my-lib"}))
        self.write("package.json", json.dumps({"workspaces": ["libs/*"], "dependencies": {"react": "^18.0.0", "my-lib": "*"}}))
        self.assertEqual(self.run_module()["cases"], 0)
        self.assertEqual(self.calls, [])

    def test_version_bump_is_not_new(self):
        self.write("requirements.txt", "requests==2.32\n")
        self.write("package.json", json.dumps({"dependencies": {"react": "^18.3.0"}}))
        self.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["httpx>=0.28"]\n')
        self.run_module()
        self.assertEqual(self.calls, [])

    def test_name_normalisation_is_not_new(self):
        self.write("requirements.txt", "Requests==2.31\n")
        self.run_module()

    def test_moving_between_manifests_is_not_new(self):
        self.write("requirements.txt", "requests==2.31\nhttpx\n")
        self.run_module()

    def test_lockfile_only_change_is_not_checked(self):
        self.write("package-lock.json", json.dumps({"packages": {"node_modules/left-pad": {}}}))
        self.write("uv.lock", "[[package]]\nname = \"idna\"\n")
        self.assertEqual(self.run_module()["cases"], 0)

    def test_decision_id_and_real_package_pass(self):
        self.write("requirements.txt", "requests==2.31\nrich>=13\n")
        out = self.run_module(KAIROS_PR_BODY="Adds rich (DEC-20261124-AB3K).")
        self.assertEqual(out["cases"], 1)
        self.assertEqual(self.calls, ["https://pypi.org/pypi/rich/json"])

    def test_tests_changed_line_does_not_clear_a_dependency(self):
        self.write("requirements.txt", "requests==2.31\nrich>=13\n")
        self.assert_flags("no decision id", KAIROS_PR_BODY="Tests-changed-because: x")

    def test_requirements_options_and_urls_are_not_names(self):
        self.write("requirements.txt", "requests==2.31\n-r other.txt\n--index-url https://x\n"
                   "-e .\ngit+https://example.com/x.git\n")
        self.run_module()

    def test_dependabot_is_ignored(self):
        self.write("requirements.txt", "requests==2.31\nrich>=13\n")
        self.assertIn("dependabot", self.run_module(KAIROS_PR_AUTHOR="dependabot[bot]")["note"])

    def test_unreachable_registry_warns_not_fails(self):
        self.write("requirements.txt", "requests==2.31\nrich>=13\n")
        with mock.patch.object(kit, "registry_status", lambda url: None):
            out = self.run_module(KAIROS_PR_BODY="DEC-1")
        self.assertTrue(any("could not reach" in w for w in out["warnings"]))

    def test_pr_opened_before_enforcement_only_warns(self):
        self.write("requirements.txt", "requests==2.31\nrich>=13\n")
        out = self.run_module(KAIROS_PR_CREATED_AT="2026-11-20T00:00:00Z")
        self.assertTrue(any("pypi:rich" in w for w in out["warnings"]))


class EvaluateTests(unittest.TestCase):
    """A module that only warns passes the gate and carries its warnings into the report."""

    def test_warnings_and_note_reach_the_report_without_failing_the_gate(self):
        suite = {"schema": 1, "profile": "p", "modules": {"fake": {}}}
        adapter = {"schema": 1, "profile": "p", "modules": {}}
        with mock.patch.dict(kit.MODULES, {"fake": lambda r, c, p: {"cases": 1, "warnings": ["w"], "note": "n"}}):
            report = kit.evaluate(tempfile.gettempdir(), dict(adapter, modules={"fake": {}}), suite)
        self.assertEqual(report["gate"], "passed")
        self.assertEqual(report["modules"][0]["warnings"], ["w"])
        self.assertEqual(report["modules"][0]["note"], "n")

    def test_production_suites_run_both_checks_after_hook_guard_and_before_repo_code(self):
        for name in ("kairos-v1", "kairos-records-v1"):
            modules = list(json.loads((ROOT / "suites" / f"{name}.json").read_text())["modules"])
            self.assertEqual(modules[1:3], ["test-tampering", "new-dependencies"], name)

    def test_workflow_passes_the_pull_request_fields_through_env(self):
        workflow = (ROOT / ".github/workflows/kairos-checks.yml").read_text()
        for key in ("KAIROS_EVENT_NAME", "KAIROS_PR_BODY", "KAIROS_PR_AUTHOR", "KAIROS_PR_CREATED_AT"):
            self.assertIn(key + ":", workflow)


if __name__ == "__main__":
    unittest.main()
