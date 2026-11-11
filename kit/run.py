"""Local prototype: run a centrally owned suite against one repository adapter.

This runs repository code. It is not a sandbox and must not receive secrets.
No network, git writes, installation, or publishing is performed by this runner.
"""
import argparse
import ast
import glob
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

VERSION = "0.1.0-prototype"


class KitError(Exception):
    pass


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def same_json(left, right):
    # Python considers False == 0 and True == 1; a wire contract must not.
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def within(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root):
        raise KitError("path escapes repository")
    return path


def command(root, argv, input_text="", timeout=30):
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
        raise KitError("command must be a nonempty argv array")
    argv = [sys.executable if x == "{python}" else x for x in argv]
    # Deliberately do not pass tokens, HOME, SSH_AUTH_SOCK, or PYTHONPATH.
    # This limits accidental inheritance; repository code is still executable code.
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP") if key in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        return subprocess.run(argv, cwd=root, env=env, input=input_text, text=True,
                              capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KitError(type(exc).__name__) from exc


def interoperability(root, config, policy):
    """Exercise a central wire contract through interchangeable producer/consumer commands."""
    cases = policy.get("cases")
    if not isinstance(cases, list) or not cases:
        raise KitError("interoperability contract has no cases")
    count = 0
    for case in cases:
        encoded = command(root, config["produce"], json.dumps(case["value"]))
        if encoded.returncode or not same_json(json.loads(encoded.stdout), case["wire"]):
            raise KitError("producer does not match the central wire fixture")
        decoded = command(root, config["consume"], json.dumps(case["wire"]))
        if decoded.returncode or not same_json(json.loads(decoded.stdout), case["value"]):
            raise KitError("consumer does not match the central wire fixture")
        invalid = case.get("invalid_wire")
        if not isinstance(invalid, list) or not invalid:
            raise KitError("contract case needs an invalid-wire fixture")
        for wire in invalid:
            rejected = command(root, config["consume"], json.dumps(wire))
            # Only an explicit protocol rejection counts. Crashes do not prove validation.
            if rejected.returncode != 2 or rejected.stdout.strip():
                raise KitError("consumer did not explicitly reject invalid wire with exit 2")
        count += 2 + len(invalid)
    return count


def modularity(root, config, policy):
    """Check declared Python import boundaries; not dynamic dependency or semantic analysis."""
    boundaries = config["boundaries"]
    if not isinstance(boundaries, list) or not boundaries:
        raise KitError("modularity has no boundaries")
    count = 0
    for boundary in boundaries:
        forbidden = boundary["forbidden_imports"]
        if not isinstance(forbidden, list) or not forbidden or not all(isinstance(x, str) and x for x in forbidden):
            raise KitError("boundary needs forbidden import prefixes")
        pattern = boundary["files"]
        within(root, pattern)
        paths = sorted(Path(p).resolve() for p in glob.glob(str(root / pattern), recursive=True))
        if not paths:
            raise KitError("boundary matched no files")
        for path in paths:
            if not path.is_relative_to(root) or not path.is_file():
                raise KitError("boundary matched a non-file or outside path")
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names = [node.module or ""] + [(node.module or "") + "." + a.name for a in node.names]
                if any(name == prefix or name.startswith(prefix + ".") for name in names for prefix in forbidden):
                    raise KitError("declared import boundary violated")
            count += 1
    return count


def repository_tests(root, config, policy):
    """Use unittest itself, with a central zero-test and skip policy, not log matching."""
    start = within(root, config["directory"])
    source = within(root, config.get("source", "."))
    if not isinstance(policy.get("allow_skips", False), bool):
        raise KitError("allow_skips must be a boolean")
    program = """import contextlib, json, pathlib, sys, unittest
sys.path.insert(0, sys.argv[2])
class Result(unittest.TextTestResult):
    completed = 0
    def addSuccess(self, test):
        super().addSuccess(test)
        self.completed += 1
with contextlib.redirect_stdout(sys.stderr):
    suite = unittest.defaultTestLoader.discover(sys.argv[1])
    result = unittest.TextTestRunner(stream=sys.stderr, resultclass=Result).run(suite)
pathlib.Path(sys.argv[3]).write_text(json.dumps({'run': result.testsRun, 'skipped': len(result.skipped),
                  'completed': result.completed, 'success': result.wasSuccessful()}), encoding='utf-8')
sys.exit(0 if result.wasSuccessful() else 1)
"""
    with tempfile.TemporaryDirectory(prefix="kit-unit-summary-") as tmp:
        summary_path = Path(tmp) / "summary.json"
        result = command(root, ["{python}", "-I", "-c", program, str(start), str(source), str(summary_path)],
                         timeout=policy.get("timeout_seconds", 120))
        if result.returncode:
            raise KitError("repository unit tests failed (details not copied into kit report)")
        summary = read_json(summary_path)
    run, skipped, completed = summary["run"], summary["skipped"], summary["completed"]
    if not summary["success"] or run < 1 or completed < 1:
        raise KitError("no executed passing unit tests")
    if skipped and not policy.get("allow_skips", False):
        raise KitError("unit tests skipped; central suite does not allow skips")
    return completed


def telosmith(root, config, policy):
    """Run Telosmith's structural records check from the kit, so no repo copies CI snippets.

    The Telosmith source tree comes from the kit's environment (KAIROS_TELOSMITH_SRC, set by
    the kit's workflow after checking out Telosmith at a pinned SHA), never from the consumer.
    Missing records fail unless the central policy says the module is optional for this profile.
    """
    src = os.environ.get("KAIROS_TELOSMITH_SRC", "")
    if not src or not (Path(src) / "aec").is_dir():
        raise KitError("Telosmith source not provided to the kit (KAIROS_TELOSMITH_SRC)")
    records = within(root, config.get("records", ".aec/records.jsonl"))
    if not records.is_file():
        raise KitError("repository has no Telosmith records")
    prefix = ["{python}", "-I", "-B", "-c",
              "import sys, runpy; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module('aec', run_name='__main__')",
              str(Path(src).resolve())]
    with tempfile.TemporaryDirectory(prefix="kit-edges-") as tmp:
        edges = Path(tmp) / "edges.jsonl"
        made = command(root, prefix + ["edges", ".", "-o", str(edges)], timeout=policy.get("timeout_seconds", 120))
        if made.returncode:
            raise KitError("Telosmith could not snapshot structural edges")
        checked = command(root, prefix + ["check", str(records), "--edges", str(edges)],
                          timeout=policy.get("timeout_seconds", 120))
    if checked.returncode:
        raise KitError("Telosmith records check reported errors")
    return sum(1 for line in records.read_text(encoding="utf-8").splitlines() if line.strip())


MODULES = {"interoperability": interoperability, "modularity": modularity,
           "repository-tests": repository_tests, "telosmith": telosmith}


def evaluate(root, adapter, suite):
    root = Path(root).resolve()
    if not isinstance(adapter, dict) or not isinstance(suite, dict):
        raise KitError("adapter and suite must be objects")
    if adapter.get("schema") != 1 or suite.get("schema") != 1:
        raise KitError("unsupported adapter or suite schema")
    if adapter.get("profile") != suite.get("profile"):
        raise KitError("adapter and suite profile differ")
    definitions = suite.get("modules")
    if not isinstance(definitions, dict) or not definitions:
        raise KitError("central suite has no modules")
    config = adapter.get("modules", {})
    if not isinstance(config, dict):
        raise KitError("adapter modules must be an object")
    if not isinstance(suite.get("profile"), str) or not suite["profile"]:
        raise KitError("suite needs a named profile")
    if set(config) - set(MODULES):
        raise KitError("adapter names an unknown module")
    results = []
    for name, policy in definitions.items():
        result = {"module": name, "status": "failed", "cases": 0}
        try:
            if name not in MODULES:
                result["status"] = "blocked"
                raise KitError("central suite names an unknown module")
            if name not in config:
                result["status"] = "blocked"
                raise KitError("required module lacks an adapter")
            if not isinstance(config[name], dict) or not isinstance(policy, dict):
                raise KitError("module configuration and policy must be objects")
            result["cases"] = MODULES[name](root, config[name], policy)
            result["status"] = "passed"
        except (KitError, KeyError, ValueError, TypeError, SyntaxError, OSError) as exc:
            # Do not echo source snippets, process output, or secrets into a shared report.
            result["reason"] = str(exc) if isinstance(exc, KitError) else type(exc).__name__
        results.append(result)
    return {"schema": "kairos-testkit/result@1", "kit_version": VERSION,
            "suite": suite["profile"], "scope": "local-prototype",
            "suite_sha256": hashlib.sha256(json.dumps(suite, sort_keys=True).encode()).hexdigest(),
            "adapter_sha256": hashlib.sha256(json.dumps(adapter, sort_keys=True).encode()).hexdigest(),
            "gate": "passed" if all(r["status"] == "passed" for r in results) else "failed",
            "modules": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--suite", required=True)
    args = parser.parse_args()
    try:
        report = evaluate(args.repo, read_json(args.adapter), read_json(args.suite))
    except (KitError, ValueError, OSError) as exc:
        report = {"gate": "failed", "reason": str(exc) if isinstance(exc, KitError) else type(exc).__name__}
    print(json.dumps(report, indent=2))
    return 0 if report["gate"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
