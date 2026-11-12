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
import re
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


def command(root, argv, input_text="", timeout=30, discard_output=False):
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x for x in argv):
        raise KitError("command must be a nonempty argv array")
    argv = [sys.executable if x == "{python}" else x for x in argv]
    # Deliberately do not pass tokens, HOME, SSH_AUTH_SOCK, or PYTHONPATH.
    # This limits accidental inheritance; repository code is still executable code.
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP") if key in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        if discard_output:
            # Only the exit code matters: unread output can't fail the run (undecodable bytes)
            # or fill memory (a long suite's log).
            return subprocess.run(argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=timeout, check=False)
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
    # Each allowed skip is scoped to named repositories and carries a reason. The repository
    # identity comes from KAIROS_REPOSITORY, which the shared workflow sets from github.repository
    # (set by Actions, not by the consumer). Unknown identity means no skip is allowed.
    entries = policy.get("allowed_skip_reasons", [])
    if not isinstance(entries, list) or not all(
            isinstance(e, dict) and isinstance(e.get("reason"), str) and e["reason"]
            and isinstance(e.get("why"), str) and e["why"]
            and isinstance(e.get("repos"), list) and e["repos"] and all(isinstance(r, str) and r for r in e["repos"])
            for e in entries):
        raise KitError("allowed_skip_reasons entries need reason, why and a nonempty repos list")
    repository = os.environ.get("KAIROS_REPOSITORY", "").lower()
    allowed_reasons = [e["reason"] for e in entries if repository and repository in {r.lower() for r in e["repos"]}]
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
                  'completed': result.completed, 'success': result.wasSuccessful(),
                  'skip_reasons': [str(r) for _, r in result.skipped]}), encoding='utf-8')
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
        # A skip passes only when the central suite names its reason (e.g. an optional external CLI).
        # Consumers can't add reasons: the allowlist lives in the central suite, not the adapter.
        reasons = summary.get("skip_reasons", [])
        unexplained = [r for r in reasons if not any(a in r for a in allowed_reasons)]
        if unexplained or len(reasons) != skipped:
            raise KitError("unit tests skipped for a reason the central suite does not allow")
    return completed


def repository_commands(root, config, policy):
    """Run the repository's own declared test commands: suites unittest discovery can't see.

    Why: some repos keep their main suite outside unittest (relay-kit's 349-case
    tests/run_tests.py, its node browser tests, a prepublish scan). Before this module the
    required kairos-checks passed without running them (Zach, 2026-11-12: "RK seems not to be
    running the Kairos checks correctly").

    Adapter: {"commands": [{"name": "regression", "argv": ["{python}", "-I", "-B", "tests/run_tests.py"]}]}
    Central policy (the suite, not the consumer, decides):
      - allowed_programs: the first argv element must be one of these ("{python}" is this Python);
      - timeout_seconds: per command;
      - optional: if true, a repo that declares no commands is reported as not-declared, not failed.
    Each command passes on exit 0. Output is never copied into the report (it may hold secrets);
    the failing command's name is. Trust note: the repo chooses its commands, exactly as it
    already chooses its unit tests; the kit guarantees they ran, not that they are thorough.
    """
    commands = config.get("commands")
    if not isinstance(commands, list) or not commands:
        raise KitError("repository-commands needs a nonempty commands list")
    allowed = policy.get("allowed_programs", ["{python}"])
    if not isinstance(allowed, list) or not all(isinstance(a, str) and a for a in allowed):
        raise KitError("allowed_programs must be a list of program names")
    names = set()
    for entry in commands:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"]:
            raise KitError("each command needs a name")
        if entry["name"] in names:
            raise KitError("command names must be unique")
        names.add(entry["name"])
        argv = entry.get("argv")
        if not isinstance(argv, list) or not argv or argv[0] not in allowed:
            raise KitError(f"command {entry['name']!r}: program not allowed by the central suite")
    for entry in commands:
        try:
            result = command(root, entry["argv"], timeout=policy.get("timeout_seconds", 600), discard_output=True)
        except KitError as exc:
            # Timeout or can't start: name the command (never its argv or output) so a
            # multi-command adapter shows which suite broke (Copilot on Lydian#6).
            raise KitError(f"command {entry['name']!r} {exc}") from exc
        if result.returncode:
            raise KitError(f"command {entry['name']!r} failed (exit {result.returncode}; output not copied)")
    return len(commands)


def hook_guard(root, config, policy):
    """Hold a repository's agent hooks to fingerprints kept centrally (Zach, 2026-11-12).

    Why: a hook command in `.claude/settings.json` (or a committed `settings.local.json`) runs
    automatically on the machine of anyone who opens an agent session in that checkout. A branch
    can change it. This check runs from Lydian, not from the branch: every hook command must match
    a SHA-256 fingerprint listed for that repository (or for every repository, under "*") in the
    central suite. A new or changed hook fails until the owner adds its fingerprint here.
    Every other setting in those files (env, statusLine, helper commands, ...) and a root
    `.mcp.json` must match fingerprints too; unknown fields in hook entries fail. It also fails if
    the repository root holds a module file, compiled file or package folder that would shadow a
    module a hook imports (aec, runpy, site, sitecustomize, ...), the trick behind Telosmith#77.

    What it can't do: vet code a hook legitimately runs from the checkout (for example a repo's
    own drift script), and it can't stop a pull request that rewrites the workflow calling it.
    Owner review of `.github/` and `.claude/` changes covers those. No adapter needed.
    """
    allowed_map = policy.get("allowed_hook_commands_sha256", {})
    allowed_extras = policy.get("allowed_settings_extras_sha256", {})
    if not isinstance(allowed_map, dict) or not isinstance(allowed_extras, dict):
        raise KitError("allowed_hook_commands_sha256 and allowed_settings_extras_sha256 must be objects")
    repository = os.environ.get("KAIROS_REPOSITORY", "").lower()
    allowed = set(allowed_map.get("*", []))
    for repo, hashes in allowed_map.items():
        if repo != "*" and repo.lower() == repository:
            allowed |= set(hashes)
    checked = 0
    # Each name is a module: its .py/.pyc file and its package folder are both blocked
    # (Copilot on #7: sitecustomize/__init__.py imports as surely as sitecustomize.py).
    # Native extensions import too (Copilot on #7: sitecustomize.cpython-311-x86_64-linux-gnu.so),
    # so any root entry named <stem> or <stem>.<...>.<py|pyc|pyo|pyw|so|pyd|dylib> counts.
    names = [e.name for e in root.iterdir()]
    for stem in policy.get("forbidden_root_modules", []):
        importable = re.compile(rf"{re.escape(stem)}(\.[^.]+)*\.(py|pyc|pyo|pyw|so|pyd|dylib)", re.I)
        for name in names:
            if name == stem or importable.fullmatch(name):
                raise KitError(f"repository root has {name!r}, which could shadow a module an agent hook imports")
        checked += 1
    extras = set(allowed_extras.get("*", []))
    for repo, hashes in allowed_extras.items():
        if repo != "*" and repo.lower() == repository:
            extras |= set(hashes)

    def canonical(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    # Aliases of the guarded paths (Codex security review on #7). Case-insensitive filesystems (macOS,
    # Windows) treat `.CLAUDE/settings.local.json` or `.MCP.json` as the real file; Windows also drops
    # trailing dots and spaces. A session started in a subfolder reads that subfolder's `.claude/` and
    # `.mcp.json`. So: any spelling of a guarded path other than the exact root one fails, anywhere.
    def norm(name):
        return name.rstrip(". ").casefold()

    settings_names = {"settings.json", "settings.local.json"}
    # No symlinks on guarded paths (Codex on #7): a symlinked root .claude points the walk below
    # at a folder whose name isn't checked, and a symlinked settings file can change under the check.
    for rel in (".claude", ".claude/settings.json", ".claude/settings.local.json", ".mcp.json"):
        if (root / rel).is_symlink():
            raise KitError("a guarded agent-settings path is a symlink")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        here = Path(dirpath)
        at_root = here == root
        for d in dirnames:
            if norm(d) == ".claude" and not (at_root and d == ".claude"):
                if any(norm(f.name) in settings_names for f in (here / d).iterdir()):
                    raise KitError("repository has agent settings outside the root .claude folder "
                                   "(a case variant, a trailing dot or space, or a subfolder)")
        if norm(here.name) == ".claude" and here.parent == root and here.name == ".claude":
            for f in filenames:
                if norm(f) in settings_names and f not in settings_names:
                    raise KitError("repository has a case or trailing-character variant of an agent-settings file")
        for f in filenames:
            if norm(f) == ".mcp.json" and not (at_root and f == ".mcp.json"):
                raise KitError("repository has an MCP config outside the exact root .mcp.json")

    # An MCP config starts programs too (Copilot on #7: anything that changes what runs counts).
    mcp = within(root, ".mcp.json")
    if mcp.is_file():
        try:
            digest = canonical(json.loads(mcp.read_text(encoding="utf-8")))
        except ValueError:
            raise KitError(".mcp.json is not valid JSON")
        if digest not in extras:
            raise KitError(f".mcp.json {digest[:12]} is not on the central allowlist")
        checked += 1
    for rel in (".claude/settings.json", ".claude/settings.local.json"):
        path = within(root, rel)
        if not path.is_file():
            continue
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            raise KitError(f"{rel} is not valid JSON")
        if not isinstance(settings, dict):
            raise KitError(f"{rel} must be a JSON object")
        # Deny by default: any setting besides hooks (env, statusLine, helpers, ...) can change what
        # runs, so each one must match a fingerprint kept here (Copilot on #7: an "env" PYTHONPATH
        # sent an approved hook to a planted package). Keys are never echoed: positions only.
        for k_index, key in enumerate(sorted(settings), 1):
            if key == "hooks":
                continue
            digest = canonical({key: settings[key]})
            if digest not in extras:
                raise KitError(f"{rel}: setting {k_index} ({digest[:12]}) besides hooks is not on the central allowlist")
            checked += 1
        hooks = settings.get("hooks", {})
        if not isinstance(hooks, dict):
            raise KitError(f"{rel}: hooks must be an object")
        for e_index, (_event, matchers) in enumerate(hooks.items(), 1):
            # The event key comes from the file, so it is never echoed: positions only.
            event = f"hook group {e_index}"
            for matcher in matchers if isinstance(matchers, list) else [None]:
                entries = matcher.get("hooks") if isinstance(matcher, dict) else None
                if isinstance(matcher, dict) and set(matcher) - {"matcher", "hooks"}:
                    raise KitError(f"{rel}: {event} has a field the guard doesn't know")
                if not isinstance(entries, list):
                    raise KitError(f"{rel}: {event} hook entry has an unexpected shape")
                for entry in entries:
                    if isinstance(entry, dict) and set(entry) - {"type", "command", "timeout"}:
                        raise KitError(f"{rel}: {event} hook has a field the guard doesn't know")
                    cmd = entry.get("command") if isinstance(entry, dict) else None
                    if not isinstance(cmd, str):
                        raise KitError(f"{rel}: {event} hook without a command string")
                    digest = hashlib.sha256(cmd.encode("utf-8")).hexdigest()
                    if digest not in allowed:
                        raise KitError(f"{rel}: {event} hook {digest[:12]} is not on the central allowlist "
                                       f"(a changed or new hook; the owner adds its fingerprint in Lydian)")
                    checked += 1
    return checked


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
    count = sum(1 for line in records.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("//"))
    if count < 1:
        raise KitError("Telosmith records file has no records")
    return count


MODULES = {"interoperability": interoperability, "modularity": modularity,
           "repository-tests": repository_tests, "repository-commands": repository_commands,
           "hook-guard": hook_guard, "telosmith": telosmith}


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
    # A module whose policy says run_first (hook-guard) inspects the checkout before any module
    # runs repository-controlled code, which could otherwise delete what it looks for
    # (Codex P1 and Copilot on Lydian#7). Other modules keep their declared order.
    ordered = sorted(definitions.items(), key=lambda kv: not (isinstance(kv[1], dict) and kv[1].get("run_first") is True))
    for name, policy in ordered:
        result = {"module": name, "status": "failed", "cases": 0}
        try:
            if name not in MODULES:
                result["status"] = "blocked"
                raise KitError("central suite names an unknown module")
            if name not in config and isinstance(policy, dict) and policy.get("optional") is True:
                result["status"] = "not-declared"
                results.append(result)
                continue
            if name not in config and isinstance(policy, dict) and policy.get("adapter_optional") is True:
                config = dict(config, **{name: {}})
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
            "gate": "passed" if results and any(r["status"] == "passed" for r in results)
                    and all(r["status"] in ("passed", "not-declared") for r in results) else "failed",
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
