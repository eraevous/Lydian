"""closing-keywords: each PR description it must flag, and clean decoys it must not.

Handoff HO-LYD-20261127-HA08. GitHub closed Lydian #27 by itself when #30 merged, because #30's
description said "It closes #27's open security finding". The module fails a PR description that
puts a closing keyword before an issue or PR reference, unless that line carries
<!-- intends-close -->. It reads only the PR body from the environment; no repository is needed.
"""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kit_run_close", ROOT / "kit/run.py")
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)


class ClosingKeywords(unittest.TestCase):
    def setUp(self):
        self.env = {"KAIROS_EVENT_NAME": "pull_request", "KAIROS_PR_AUTHOR": "eraevous",
                    "KAIROS_PR_CREATED_AT": "2026-12-01T00:00:00Z", "KAIROS_PR_BODY": ""}
        self.policy = {"enforce_from": "2026-11-28T00:00:00Z"}
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def run_module(self, body, **env):
        with mock.patch.dict(os.environ, {**self.env, "KAIROS_PR_BODY": body, **env}, clear=False):
            return kit.MODULES["closing-keywords"](Path(self.temp.name), {}, self.policy)

    def assert_flags(self, body, *fragments, **env):
        with self.assertRaises(kit.KitError) as caught:
            self.run_module(body, **env)
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))
        return str(caught.exception)

    # ---- cases it must flag ----

    def test_mid_sentence_closes_fails(self):
        # The #30 wording that closed #27.
        message = self.assert_flags("Fix the guard.\n\nIt closes #27's open security finding.\n",
                                    "line 3", "closes #27", "<!-- intends-close -->")
        self.assertIn("reword", message)

    def test_every_keyword_form_fails_in_any_case(self):
        for word in ("close", "closes", "closed", "fix", "fixes", "fixed",
                     "resolve", "resolves", "resolved", "CLOSES", "Fixes", "ReSoLvEd"):
            with self.subTest(word=word):
                self.assert_flags(f"This {word} #12.", f"{word} #12")

    def test_colon_after_keyword_fails(self):
        self.assert_flags("Closes: #8", "Closes: #8")

    def test_cross_repository_reference_fails(self):
        self.assert_flags("Fixes eraevous/relay-kit#110 too.", "eraevous/relay-kit#110")

    def test_issue_and_pull_urls_fail(self):
        for url in ("https://github.com/eraevous/Lydian/issues/27", "https://github.com/eraevous/Lydian/pull/30"):
            with self.subTest(url=url):
                self.assert_flags(f"resolves {url}", url)

    def test_every_offending_line_is_named(self):
        message = self.assert_flags("closes #1\nok\nfixes #2", "line 1", "line 3")
        self.assertIn("2 line(s)", message)

    def test_marker_on_another_line_does_not_clear(self):
        self.assert_flags("closes #27\n<!-- intends-close -->", "line 1")

    def test_unknown_creation_time_is_enforced(self):
        self.assert_flags("closes #27", "line 1", KAIROS_PR_CREATED_AT="")

    # ---- clean decoys it must not flag ----

    def test_marked_line_passes(self):
        result = self.run_module("Closes #27 <!-- intends-close -->\nAlso fixes #28. <!--intends-close-->")
        self.assertNotIn("warnings", result)
        self.assertIn("2 intended", result["note"])

    def test_plain_reference_passes(self):
        result = self.run_module("Follows #27. See #30 and eraevous/Lydian#31.")
        self.assertEqual(result, {"cases": 1})

    def test_keyword_not_directly_before_reference_passes(self):
        for body in ("The bug fixed in #27 stays fixed.", "Addresses #27.", "prefixes #3", "closest #4",
                     "unresolved #5", "Fixes the flake (see #6).", "closes\n#7"):
            with self.subTest(body=body):
                self.assertEqual(self.run_module(body), {"cases": 1})

    def test_empty_body_passes(self):
        self.assertEqual(self.run_module(""), {"cases": 1})

    def test_not_a_pull_request_checks_nothing(self):
        result = self.run_module("closes #27", KAIROS_EVENT_NAME="push")
        self.assertEqual(result["cases"], 0)
        self.assertIn("not a pull request", result["note"])

    def test_needs_no_base_revision(self):
        # Unlike the other PR guards, a description check has nothing to compare.
        self.assertEqual(self.run_module("Plain.", KAIROS_BASE_SHA=""), {"cases": 1})

    def test_opened_before_enforcement_only_warns(self):
        result = self.run_module("closes #27", KAIROS_PR_CREATED_AT="2026-11-27T00:00:00Z")
        self.assertIn("warning only", result["note"])
        self.assertTrue(any("closes #27" in w for w in result["warnings"]))

    def test_dependabot_is_exempt(self):
        # Its bodies quote upstream changelogs ("Fixes #123" about the upstream repository).
        result = self.run_module("Fixes #123", KAIROS_PR_AUTHOR="dependabot[bot]")
        self.assertIn("exempt", result["note"])

    def test_long_line_is_shortened_in_the_report(self):
        message = self.assert_flags("x" * 500 + " closes #27 " + "y" * 500, "closes #27")
        self.assertLess(len(message), 900)


class InSuites(unittest.TestCase):
    def test_both_production_suites_run_it_without_an_adapter_entry(self):
        for name in ("kairos-v1", "kairos-records-v1"):
            with self.subTest(suite=name):
                policy = json.loads((ROOT / f"suites/{name}.json").read_text())["modules"]["closing-keywords"]
                self.assertIs(policy.get("adapter_optional"), True)
                self.assertRegex(policy.get("enforce_from", ""), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


if __name__ == "__main__":
    unittest.main()
