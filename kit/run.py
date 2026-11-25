"""Run a central Lydian suite against one repository's adapter and print a JSON report.

The shared kairos-checks workflow runs this for every calling repository: the suite (suites/*.json)
names the modules and their policy; the repository's .kairos/adapter.json only says how it differs.
Exit 0 when the gate passes, 1 otherwise.

This runs repository code. It is not a sandbox and must not receive secrets.
No git writes, installation, or publishing is performed by this runner. Its only network use is
the new-dependencies module's registry lookups (PyPI, npm) for packages a pull request adds.
The report's "scope": "local-prototype" and kit_version are report fields callers may read, kept
as they are; they don't describe where the kit runs.
"""
import argparse
import ast
import glob
import hashlib
import json
import os
import posixpath
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import zlib

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
    allow_skips = policy.get("allow_skips", False)
    if not isinstance(allow_skips, bool):
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
    if skipped and not allow_skips:
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


# YAML line breaks plus the Unicode ones Python's splitlines() honours: keys are looked for on every
# piece any of them could start.
LINE_BREAKS = re.compile(r"[\r\x0b\x0c\x1c-\x1e\x85  ]")
# The only YAML escapes that can spell a letter (\x, \u, \U in a double-quoted scalar) and an escaped
# line break, which joins two lines with nothing between them ("hoo\<newline>ks" is hooks).
LETTER_ESCAPE = re.compile(r"\\(?:[xuU]|$)")
# A YAML tag (a "!" starting a token) can construct a key from other text: "!!binary aG9va3M=" is
# hooks in base64 (Codex security review on #12). Skill frontmatter has no use for tags.
YAML_TAG = re.compile(r"(?:^|[\s\[\]{},:?-])!")


# Anything a Git LFS pointer could contain, anywhere in the file: git-lfs also accepts noncanonical
# pointers (leading blank lines, the legacy hawser spec URL), so matching byte zero isn't enough
# (Codex security review on #12). A skill that merely mentions git-lfs fails too.
LFS_POINTER = re.compile(rb"(?i)git-lfs|hawser|\boid\s+sha256\s*:")


def frontmatter_declares_hooks(path):
    """True if a markdown file's YAML frontmatter could hold a hooks key.

    Claude Code runs hooks declared in skill and subagent frontmatter (code.claude.com/docs/en/hooks,
    "Hooks in skills and agents"; the format is the settings one). Lydian keeps no fingerprints for
    them, so any frontmatter that could declare one fails: a hook that must exist goes in
    .claude/settings.json, where it is checked.

    No YAML parser (no third-party dependencies), so the rule doesn't try to tell keys from values:
    the frontmatter fails if the word "hooks" appears anywhere in it, in any case, or if it uses an
    escape that could spell a letter or join two lines, or a YAML tag (which can build a key from
    other text, as !!binary does). Key-recognition rules kept being bypassed by
    YAML's other key forms (escaped, explicit "?", flow, tagged; Codex on #12); a key named hooks
    has to contain those five letters in the raw text unless an escape builds them. The cost: a
    description that mentions "hooks" fails too, and gets reworded.

    The opening line may carry spaces, but only an exact "---" line (split on "\\n" alone) closes the
    frontmatter, so no other line break, "--- " line or "..." can end it early for this check while
    Claude Code reads on. Frontmatter that never closes is all frontmatter.
    """
    resolved = path.resolve()
    if not resolved.is_file():
        raise KitError("a markdown file under .claude is not a regular file")
    # Bytes, not read_text: universal newlines would turn a bare "\r" into a line end and let
    # "x\r---" pass for the closing line (Copilot on #12).
    text = resolved.read_bytes().decode("utf-8", errors="replace").lstrip("\ufeff")
    lines = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line == "---":
            return False
        if "hooks" in line.casefold():
            return True
        if any(LETTER_ESCAPE.search(piece) or YAML_TAG.search(piece) for piece in LINE_BREAKS.split(line)):
            return True
    return False


def fingerprints_for(mapping, repository):
    """The fingerprints a suite allows for one repository: those under "*" plus its own entry.

    Repository names compare case-insensitively; `repository` is already lowercased.
    """
    allowed = set(mapping.get("*", []))
    for repo, hashes in mapping.items():
        if repo != "*" and repo.lower() == repository:
            allowed |= set(hashes)
    return allowed


def canonical(value):
    """SHA-256 of a JSON value in canonical form (sorted keys, no spaces): a settings fingerprint."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def norm(name):
    """A path component as a case-insensitive filesystem may read it.

    macOS and Windows treat `.CLAUDE/settings.local.json` or `.MCP.json` as the real file, and Win32
    path normalization drops trailing dots and spaces, so `.claude.` may name `.claude` there (handled
    by string normalization only; not demonstrated on a Windows checkout).
    """
    return name.rstrip(". ").casefold()


def hook_guard(root, config, policy):
    """Hold a repository's agent hooks to fingerprints kept centrally (Zach, 2026-11-12).

    Why: a hook command in `.claude/settings.json` (or a committed `settings.local.json`) runs
    automatically on the machine of anyone who opens an agent session in that checkout. A branch
    can change it. This check runs from Lydian, not from the branch: every hook command must match
    a SHA-256 fingerprint listed for that repository (or for every repository, under "*") in the
    central suite. A new or changed hook fails until the owner adds its fingerprint here.
    Every other setting in those files (env, statusLine, helper commands, ...) and a root
    `.mcp.json` must match fingerprints too; unknown fields in hook entries fail. Hooks declared in the
    frontmatter of a skill, subagent or command file under any `.claude` folder fail outright: the
    guard keeps no fingerprints for them. It also fails if
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
    allowed = fingerprints_for(allowed_map, repository)
    extras = fingerprints_for(allowed_extras, repository)
    # Three stages, in this order: root modules a hook could import, the shape of every agent-settings
    # path in the tree, then the fingerprints of what the root settings files and .mcp.json start.
    names = [e.name for e in root.iterdir()]
    checked = guard_root_modules(names, policy.get("forbidden_root_modules", []))
    guard_agent_paths(root, names)
    return checked + guard_fingerprints(root, allowed, extras)


def guard_root_modules(names, stems):
    """Fail if a root entry could shadow a module an agent hook imports; one case per stem."""
    # Each name is a module: its .py/.pyc file and its package folder are both blocked
    # (Copilot on #7: sitecustomize/__init__.py imports as surely as sitecustomize.py).
    # Native extensions import too (Copilot on #7: sitecustomize.cpython-311-x86_64-linux-gnu.so),
    # so any root entry named <stem> or <stem>.<...>.<py|pyc|pyo|pyw|so|pyd|dylib> counts.
    checked = 0
    for stem in stems:
        importable = re.compile(rf"{re.escape(stem)}(\.[^.]+)*\.(py|pyc|pyo|pyw|so|pyd|dylib)", re.I)
        for name in names:
            # Case-insensitive like the regex: AEC/ and Aec/ import as aec on case-insensitive filesystems
            # (Abigail's probe of 0743ae2: a bare AEC/__init__.py package passed).
            if norm(name) == stem.casefold() or importable.fullmatch(name.rstrip(". ")):
                raise KitError(f"repository root has {name!r}, which could shadow a module an agent hook imports")
        checked += 1
    return checked


def guard_agent_paths(root, names):
    """Fail on any agent-settings path the fingerprint stage wouldn't read: aliases, symlinks,
    submodules, LFS pointers and frontmatter hooks. Counts no cases."""
    # Aliases of the guarded paths (Codex security review on #7): see norm(). A session started in a
    # subfolder reads that subfolder's `.claude/` and `.mcp.json`. So: any spelling of a guarded path
    # other than the exact root one fails, anywhere.
    settings_names = {"settings.json", "settings.local.json"}
    # No symlinks on guarded paths (Codex on #7): a symlinked root .claude points the walk below
    # at a folder whose name isn't checked, and a symlinked settings file can change under the check.
    # A submodule is an empty folder in CI (checkout doesn't fetch it), so its .claude content,
    # skills included, is never scanned here; any submodule fails (Codex security review on #12).
    if any(norm(name) == ".gitmodules" for name in names):
        raise KitError("repository declares git submodules, whose agent settings and skills the guard can't inspect")
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
        in_claude = any(norm(part) == ".claude" for part in here.relative_to(root).parts)
        # Claude Code follows symlinked skill folders (code.claude.com/docs/en/skills) and os.walk
        # doesn't descend into them, so no symlink is allowed inside any .claude folder, nor as one
        # (Codex on #12).
        for name in dirnames + filenames:
            if (in_claude or norm(name) == ".claude") and (here / name).is_symlink():
                raise KitError("a path inside a .claude folder is a symlink")
        for f in filenames:
            if norm(f) == ".mcp.json" and not (at_root and f == ".mcp.json"):
                raise KitError("repository has an MCP config outside the exact root .mcp.json")
            # CI checks out without Git LFS, so a file committed through LFS is only a pointer here and
            # becomes the real file (a hook-bearing skill, say) on a developer's checkout (Copilot on #12).
            if in_claude and (here / f).is_file():
                with open(here / f, "rb") as fh:
                    if LFS_POINTER.search(fh.read()):
                        raise KitError("a file under .claude is a Git LFS pointer, whose real contents the guard can't inspect")
            if in_claude and norm(f).endswith(".md") and frontmatter_declares_hooks(here / f):
                # The file's path is repository text, so it is not echoed.
                raise KitError("a skill, subagent or command file under .claude declares hooks in its "
                               "frontmatter; the guard can't fingerprint those, so declare the hook in "
                               ".claude/settings.json")


def guard_fingerprints(root, allowed, extras):
    """Hold the root .mcp.json, every non-hook setting and every hook command to a central fingerprint.

    `allowed` holds hook-command fingerprints, `extras` the rest. Returns the number checked.
    """
    checked = 0
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


CODE_IMPACT_LIMIT = 60_000   # characters kept in the job summary (GitHub allows 1 MiB per step)


def code_impact(root, prefix, records, edges, tmp, base, timeout):
    """Telosmith code-impact against the PR base: structural, report-only, never a gate (DEC-256).

    Same terms as the aec.yml it replaces: the base is the merge base of the event's base revision
    and HEAD; with none, or one that can't be snapshotted, it says it was skipped. The text goes to
    the job summary through a file the kit's workflow names (KAIROS_CODE_IMPACT_OUT), not into the
    JSON report. Returns the markdown to write; it never raises for a code-impact problem.
    """
    def skipped(why):
        return f"code-impact skipped: {why}\n"
    # The base comes from the event (a full SHA), never from the repository; anything else is skipped,
    # so a value can't be read as a git option or a symbolic ref.
    if not re.fullmatch(r"[0-9a-f]{40}", base or "") or set(base) == {"0"}:
        return skipped("no base revision for this event")
    try:
        found = command(root, ["git", "merge-base", base, "HEAD"], timeout=timeout)
        merge_base = found.stdout.strip()
        if found.returncode or not re.fullmatch(r"[0-9a-f]{40}", merge_base):
            return skipped("no base revision for this event")
        before = tmp / "before.jsonl"
        snap = command(root, prefix + ["snapshot", ".", "--rev", merge_base, "-o", str(before)], timeout=timeout)
        if snap.returncode:
            return skipped(f"could not snapshot {merge_base}")
        ran = command(root, prefix + ["code-impact", str(records), str(before), str(edges)], timeout=timeout)
    except KitError as exc:
        return skipped(str(exc))
    except UnicodeDecodeError:
        # A record can hold an escaped lone surrogate the check accepts but stdout can't encode.
        return skipped("output was not valid UTF-8")
    if ran.returncode:
        # Run without --exit-code, so a nonzero exit is an execution error, not detected impact.
        return skipped(f"Telosmith code-impact exited {ran.returncode}")
    text = (ran.stdout + ran.stderr).strip()
    if len(text) > CODE_IMPACT_LIMIT:
        text = text[:CODE_IMPACT_LIMIT] + "\n... (truncated)"
    # The text quotes repository records verbatim, so it can hold a backtick line that would close a fixed
    # fence and let the rest render as markdown in the summary. A fence longer than any backtick run in
    # the text can't be closed from inside (CommonMark: closing needs at least as many backticks).
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(4, longest + 1)
    return (f"## Telosmith code-impact (structural, report-only) vs {merge_base}\n"
            f"{fence}\n{text}\n{fence}\n")


def telosmith(root, config, policy):
    """Run Telosmith's structural records check from the kit, so no repo copies CI snippets.

    The Telosmith source tree comes from the kit's environment (KAIROS_TELOSMITH_SRC, set by
    the kit's workflow after checking out Telosmith at a pinned SHA), never from the consumer.
    Missing records fail unless the central policy says the module is optional for this profile.
    When the workflow names KAIROS_CODE_IMPACT_OUT, a passing check is followed by code_impact().
    """
    src = os.environ.get("KAIROS_TELOSMITH_SRC", "")
    if not src or not (Path(src) / "aec").is_dir():
        raise KitError("Telosmith source not provided to the kit (KAIROS_TELOSMITH_SRC)")
    records = within(root, config.get("records", ".aec/records.jsonl"))
    if not records.is_file():
        raise KitError("repository has no Telosmith records")
    timeout = policy.get("timeout_seconds", 120)
    prefix = ["{python}", "-I", "-B", "-c",
              "import sys, runpy; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module('aec', run_name='__main__')",
              str(Path(src).resolve())]
    with tempfile.TemporaryDirectory(prefix="kit-edges-") as tmp:
        edges = Path(tmp) / "edges.jsonl"
        made = command(root, prefix + ["edges", ".", "-o", str(edges)], timeout=timeout)
        if made.returncode:
            raise KitError("Telosmith could not snapshot structural edges")
        checked = command(root, prefix + ["check", str(records), "--edges", str(edges)], timeout=timeout)
        if checked.returncode:
            raise KitError("Telosmith records check reported errors")
        out = os.environ.get("KAIROS_CODE_IMPACT_OUT", "")
        if out:
            report = code_impact(root, prefix, records, edges, Path(tmp),
                                 os.environ.get("KAIROS_BASE_SHA", ""), timeout)
            try:
                Path(out).write_text(report, encoding="utf-8")
            except OSError:
                pass   # report-only: a summary that can't be written never fails the gate
    count = sum(1 for line in records.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("//"))
    if count < 1:
        raise KitError("Telosmith records file has no records")
    return count


# ---- Pull-request guards: test-tampering and new-dependencies (HO-LYD-20261124-SGZN) ----------
# Both read the pull request from the environment the shared workflow sets from the event payload
# (never from the repository): KAIROS_EVENT_NAME, KAIROS_BASE_SHA, KAIROS_PR_BODY, KAIROS_PR_AUTHOR,
# KAIROS_PR_CREATED_AT. Outside a pull request they check nothing and say so.

# A decision id as a whole token: DEC-<n> (up to DEC-272) or DEC-YYYYMMDD-XXXX (DEC-272 on).
DECISION_ID = re.compile(r"(?<![\w-])DEC-(?:\d{8}-[0-9A-HJKMNP-TV-Z]{4}|\d{1,4})(?![\w-])")
TESTS_CHANGED = re.compile(r"(?m)^Tests-changed-because:[ \t]*\S")
# Jest also runs any JS/TS file under a __tests__ folder, with no .test suffix (Copilot on #29).
# unittest's default discovery loads test*.py (not only test_*.py) (Codex security review on #29).
TEST_FILE = re.compile(r"(?:^|/)(?:test[^/]*\.py|[^/]*_test\.py|tests\.py|[^/]*\.(?:test|spec)\.[cm]?[jt]sx?)$"
                       r"|(?:^|/)__tests__/(?:[^/]+/)*[^/]+\.[cm]?[jt]sx?$")
# Whitespace may sit around a member-access dot (and a stripped comment leaves a space): "test. only("
# still runs, so every pattern allows it (Codex on #29).
PY_ASSERTS = re.compile(r"(?<![\w.])assert\b|\.\s*assert[A-Z_]\w*\s*\(")
PY_SKIPS = re.compile(r"\bmark\s*\.\s*(?:skip|skipif|xfail)\b|\bunittest\s*\.\s*(?:skip|skipIf|skipUnless|expectedFailure)\b"
                      r"|\bpytest\s*\.\s*(?:skip|xfail)\s*\(|\.\s*skipTest\s*\(")
JS_ASSERTS = re.compile(r"(?<![\w.$])expect\s*\(|(?<![\w.$])assert\b")
JS_SKIPS = re.compile(r"\.\s*(?:skip|skipIf|only|todo)(?=\s*[.(])|(?<![\w.$])x(?:it|test|describe)(?=\s*[.(])")   # also .only.each(...) (Codex on #29)
BOTS_EXEMPT = ("dependabot[bot]",)


def pull_request_context(policy):
    """The pull request under check, or a note why there is none. Fails closed on a PR with no base."""
    if os.environ.get("KAIROS_EVENT_NAME", "") != "pull_request":
        return None, {"cases": 0, "note": "not a pull request: nothing to compare"}
    author = os.environ.get("KAIROS_PR_AUTHOR", "")
    if author in BOTS_EXEMPT:
        return None, {"cases": 0, "note": f"{author}: dependabot pull requests are exempt"}
    base = os.environ.get("KAIROS_BASE_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base):
        raise KitError("pull request without a base revision: cannot compare")
    enforce_from = policy.get("enforce_from", "")
    created = os.environ.get("KAIROS_PR_CREATED_AT", "")
    # ISO-8601 UTC strings of one shape compare as text. A PR older than enforce_from only warns
    # (no retroactive blocking); an unknown creation time is enforced.
    warn_only = bool(enforce_from and created and created < enforce_from)
    return {"base": base, "body": os.environ.get("KAIROS_PR_BODY", "") or "", "warn_only": warn_only}, None


def git_out(root, *args):
    try:
        done = subprocess.run(["git", "-c", "core.quotePath=false", "-C", str(root), *args],
                              env={k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR") if k in os.environ},
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KitError(f"git {args[0]}: {type(exc).__name__}") from exc
    if done.returncode:
        raise KitError(f"git {args[0]} failed")
    return done.stdout


def file_at(root, rev, path):
    """A file's text at a revision, or None when it doesn't exist there."""
    listed = git_out(root, "ls-tree", "-z", rev, "--", path).split(b"\0")
    if not listed or not listed[0]:
        return None
    if listed[0].split(b" ", 1)[0] in (b"120000", b"160000"):
        # git show of a symlink returns its target path, not what a checkout reads through it, so a
        # guarded file can't be a link (Codex security review on #29). A submodule has no text here.
        raise KitError(f"{path}: a symlink or submodule; test files and manifests must be regular files")
    return git_out(root, "show", f"{rev}:{path}").decode("utf-8", "replace")


def tracked(root, rev):
    return [p for p in git_out(root, "ls-tree", "-r", "-z", "--name-only", rev).decode("utf-8", "replace").split("\0") if p]


def guard_result(findings, ctx, cleared_by, cases, how):
    if not findings:
        return {"cases": cases}
    if cleared_by:
        return {"cases": cases, "note": f"{len(findings)} finding(s) cleared by the PR body ({cleared_by})",
                "warnings": findings}
    if ctx["warn_only"]:
        return {"cases": cases, "note": "pull request opened before enforcement: warning only",
                "warnings": findings}
    raise KitError("; ".join(findings) + f". To accept: {how}")


REGEX_KEYWORDS = ("return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw",
                  "case", "do", "else", "yield", "await")


def _function_expression_head(before):
    """True when `before` ends with the head of a function expression (`= function name(...)`,
    `(async function* (...)`): a `function` keyword whose parameter list closes `before`, in a
    place where an expression is expected, not at the start of a statement."""
    depth = 0
    for k in range(len(before) - 1, -1, -1):
        if before[k] == ")":
            depth += 1
        elif before[k] == "(":
            depth -= 1
            if depth == 0:
                head = re.search(r"(?:\basync\s+)?\bfunction\b\s*\*?\s*(?:[A-Za-z_$][\w$]*)?\s*$", before[:k])
                if not head:
                    return False
                lead = before[:head.start()].rstrip()
                if not lead or lead.endswith((";", "{", "}", ")")):
                    return False   # a declaration: function f() {} starts and ends a statement
                word = re.search(r"[A-Za-z_$][\w$]*$", lead)
                return not word or word.group(0) in REGEX_KEYWORDS   # return function () {}, x = function...
    return False


def _regex_can_start(code):
    """True when a "/" after `code` begins a JS regex literal rather than a division: after an
    operator, an opening bracket, a comma or semicolon, a keyword, or at the start."""
    stripped = code.rstrip()
    if not stripped:
        return True
    if stripped.endswith(("++", "--")) and re.search(r"[\w$)\]]\s*(?:\+\+|--)$", stripped):
        return False   # postfix x++ / 2 is division (Codex on #29)
    if stripped[-1] in "(,=:[!&|?{;+-*%<>~^":
        return True
    if stripped[-1] == "}":
        # After a block, a "/" starts a statement, so it's a regex; after an object literal
        # ({} / 2) it's division (Copilot on #29). Find the matching "{" and look before it.
        depth = 0
        for k in range(len(stripped) - 1, -1, -1):
            if stripped[k] == "}":
                depth += 1
            elif stripped[k] == "{":
                depth -= 1
                if depth == 0:
                    before = stripped[:k].rstrip()
                    if before.endswith(")") and _function_expression_head(before):
                        return False   # function () {} / 2: the body ends an expression, so "/" divides (Codex on #29)
                    if not before or before.endswith(("=>", ")", ";", "{", "}")):
                        return True
                    # A declaration body (class X {}, class X extends Y {}, TS interface/enum/
                    # namespace) is a block too (Codex on #29).
                    if re.search(r"\b(?:class|interface|enum|namespace|module)\b[^{};=()]*$", before):
                        return True
                    word = re.search(r"[A-Za-z_$][\w$]*$", before)
                    return bool(word) and word.group(0) in ("else", "try", "finally", "do")
        return True
    if stripped[-1] == ")":
        # After the condition of if/while/for/with, a "/" starts the body, so it's a regex
        # (Codex on #29). Find the matching "(" in the scanned code (comments and strings are
        # already gone) and look at the word before it.
        depth = 0
        for k in range(len(stripped) - 1, -1, -1):
            if stripped[k] == ")":
                depth += 1
            elif stripped[k] == "(":
                depth -= 1
                if depth == 0:
                    before = re.search(r"[A-Za-z_$][\w$]*$", stripped[:k].rstrip())
                    return bool(before) and before.group(0) in ("if", "while", "for", "with")
        return False
    word = re.search(r"[A-Za-z_$][\w$]*$", stripped)
    return bool(word) and word.group(0) in REGEX_KEYWORDS


def _scan_code(text, i, python, nested):
    """Scan from i, keeping code and dropping comments and string contents. Code inside a JS
    template's ${...} or a Python f-string's {...} runs, so it is scanned as code (Codex on #29).
    With nested set, stop at the "}" that closes the interpolation. Returns (code, next index)."""
    out, n, depth, brackets, spec = [], len(text), 0, 0, False
    while i < n:
        c = text[i]
        if nested and python and spec:
            # The format spec ({value:#x}) is text, not code: dropped, so "#" there isn't a comment
            # and its words can't forge a marker. A nested field in it ({x:{width}}) is an
            # expression, scanned as code (Codex on #29).
            if c == "}":
                return "".join(out), i + 1
            if c == "{":
                code, i = _scan_code(text, i + 1, python, True)
                out.append(" " + code + " ")
                continue
            i += 1
            continue
        if nested:
            if c == "{":
                depth += 1
            elif c == "}":
                if depth == 0:
                    return "".join(out), i + 1
                depth -= 1
            if python and depth == 0:
                if c in "([":
                    brackets += 1
                elif c in ")]" and brackets:
                    brackets -= 1
                elif c == ":" and brackets == 0 and text[i + 1:i + 2] != "=":
                    # The spec starts at the first ":" outside any brackets (not ":="); before it,
                    # "#" starts a real comment even when a "}" follows on its line (Codex on #29).
                    spec = True
        if python and c == "#":
            i = text.find("\n", i)
            i = n if i < 0 else i
            continue
        if not python and text.startswith("//", i):
            i = text.find("\n", i)
            i = n if i < 0 else i
            continue
        if not python and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
            continue
        if not python and c == "/" and _regex_can_start("".join(out)):
            # A regex literal: skip it whole, so a quote inside (/can't/) isn't read as a string
            # that swallows the code after it (Codex security review on #29).
            j, in_class = i + 1, False
            while j < n and text[j] != "\n":
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "[":
                    in_class = True
                elif text[j] == "]":
                    in_class = False
                elif text[j] == "/" and not in_class:
                    break
                j += 1
            if j < n and text[j] == "/":
                j += 1
                while j < n and text[j].isalpha():
                    j += 1
                out.append("/r/")
                i = j
                continue
        if c in ("'\"" if python else "'\"`"):
            close = text[i:i + 3] if python and text[i:i + 3] in ('"""', "\'\'\'") else c
            prefix = re.search(r"[A-Za-z]*$", text[max(0, i - 3):i]).group(0).lower() if python else ""
            interpolated = c == "`" or "f" in prefix
            j = i + len(close)
            mark = len(out)
            out.append('"')
            single_line, broken = close in ("'", '"'), False
            while j < n and not text.startswith(close, j):
                if single_line and text[j] == "\n":
                    # A '...' or "..." string can't run past its line (JS or Python), so this quote
                    # didn't open one: the scanner misread what came before it (say a regex after
                    # if (...)). Read the quote as code and carry on, so nothing after it is hidden
                    # (Codex on #29).
                    broken = True
                    break
                if text[j] == "\\":
                    j += 2
                elif interpolated and not python and text.startswith("${", j):
                    code, j = _scan_code(text, j + 2, python, True)
                    out.append(" " + code + " ")
                elif interpolated and python and text.startswith("{{", j):
                    j += 2
                elif interpolated and python and text[j] == "{":
                    code, j = _scan_code(text, j + 1, python, True)
                    out.append(" " + code + " ")
                else:
                    j += 1
            if broken:
                del out[mark:]
                out.append(c)
                i += 1
                continue
            # Keep a digest of the literal, not its text: two tests' names stay distinct (so a
            # marker moved from one test to another is seen) and hex can't spell a marker.
            out.append("%08x\"" % zlib.crc32(text[i:j + len(close)].encode("utf-8", "replace")))
            i = j + len(close)
            continue
        out.append(c)
        i += 1
    return "".join(out), i


def code_only(text, python):
    """The text with comments removed and string literals emptied (interpolated code kept), by a
    small scanner: a regex can't tell a string holding "/*" from a comment (Codex on #29), so
    neither comments nor strings can hide or forge an assertion or a focus marker. Not a full lexer:
    a JS regex literal holding a quote can still confuse it."""
    return _scan_code(text, 0, python, False)[0]


def tampering_counts(path, text):
    """(assertions, skips) in one test file, by language, counted in code only: comments and string
    contents don't count, so "assert True  # assert f() == 2" is one assertion. A focused test
    (.only) disables its siblings, so it counts as a skip."""
    if path.endswith(".py"):
        text = code_only(text, True)
        return len(PY_ASSERTS.findall(text)), len(PY_SKIPS.findall(text))
    text = code_only(text, False)
    return len(JS_ASSERTS.findall(text)), len(JS_SKIPS.findall(text))


def _describe_spans(code):
    """(start, end, name) for each describe()/suite()/context() block in scanned JS code: from the
    call to the brace that closes its callback body, named by its title's string digest."""
    spans = []
    # A tagged-template table (describe.each`...`) is a string digest before the call (Codex
    # security review on #29).
    for d in re.finditer(r'(?<![\w.$])(?:describe|suite|context)\b[\s.\w]*(?:"[^(){};]*?[0-9a-f]{8}"\s*)?\(', code):
        # Follow chained calls (describe.each([...])("A", ...)) past their data to the real
        # callback, and take the title from the call's own arguments (Codex on #29).
        call = d.end() - 1
        args = _registration_args(code[call:call + 4000])
        title = " ".join(re.findall(r'[0-9a-f]{8}"', args))
        body = code.find("{", call + len(args))
        if body < 0:
            continue
        depth, k = 0, body
        while k < len(code):
            if code[k] == "{":
                depth += 1
            elif code[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        spans.append((d.start(), k, title))
    return spans


def _enclosing_class(code, pos):
    """The class a definition at `pos` sits in: the nearest `class` line above it with less
    indentation, provided no line between them dedents to the class's level or further."""
    line_start = code.rfind("\n", 0, pos) + 1
    line = code[line_start:code.find("\n", line_start) if "\n" in code[line_start:] else len(code)]
    indent = len(line) - len(line.lstrip(" \t"))
    rows = code[:line_start].splitlines()
    for row in reversed(rows):
        if not row.strip():
            continue
        row_indent = len(row) - len(row.lstrip(" \t"))
        if row_indent < indent:
            c = re.match(r"[ \t]*class\s+(\w+)", row)
            if c:
                return c.group(1)
            indent = row_indent   # an enclosing block that isn't a class: keep looking above it
            if indent == 0:
                return ""
    return ""


def _registration_args(rest):
    """The registration's own arguments: from after the marker up to its callback body (=>,
    function, {) or the end of its call, whichever comes first, so the strings of a following
    statement aren't read into a callback-less registration such as test.todo("x"); (Codex on
    #29). Chained calls (skipIf(cond)("name", ...), .each([...])(...)) are followed through."""
    stack, opened = [], False
    for k, ch in enumerate(rest):
        # `function` only as a whole keyword: an identifier like functionUnavailable is part of the
        # arguments (Codex on #29).
        keyword = (rest.startswith("function", k) and not (k and (rest[k - 1].isalnum() or rest[k - 1] in "_$"))
                   and not re.match(r"[\w$]", rest[k + 8:k + 9]))
        # Inside .each([...]) data, { and => belong to the data (Codex on #29); outside any
        # array they start the callback body.
        in_data = "[" in stack
        if not in_data and (rest.startswith("=>", k) or ch == "{" or keyword):
            return rest[:k]
        if ch in "([{":
            stack.append(ch)
            opened = True
        elif ch in ")]}":
            if stack:
                stack.pop()
            if not stack and opened and ch != "}":
                nxt = rest[k + 1:].lstrip()
                if not nxt.startswith(("(", ".")):
                    return rest[:k + 1]
        elif ch == ";" and not stack:
            return rest[:k]
    return rest


def marker_lines(path, text):
    """Each skip or focus marker's registration, counted: the code up to the marker plus the test's
    name (a string digest in JS, the next def in Python), not the test's body, so editing a skipped
    test's body isn't read as a new skip (Codex on #29) while moving a marker to another test is
    (Copilot on #29). Comments are gone and strings digested (code_only)."""
    from collections import Counter
    python = path.endswith(".py")
    pattern = PY_SKIPS if python else JS_SKIPS
    code = code_only(text, python)
    marks = Counter()
    # Match over the whole file, not line by line: the counting pattern spans line breaks
    # (test.\n  skip(...)), so the identity must too (Codex on #29).
    describe_spans = [] if python else _describe_spans(code)
    for m in pattern.finditer(code):
        line_start = code.rfind("\n", 0, m.start()) + 1
        head = re.sub(r"\s+", " ", code[line_start:m.end()].strip())
        after = code[m.end():m.end() + 4000]
        if python:
            if head.startswith("@"):
                # A decorator belongs to the definition below it, a class or a function: a
                # class-level skip is the class's, not its first method's (Codex on #29).
                d = re.search(r"\b(class|def)\s+(\w+)", after)
                name = (d.group(2) if d.group(1) == "def" else f"class {d.group(2)}") if d else ""
                def_at = m.end() + d.start() if d else None
            else:
                # A runtime skip (pytest.skip(), self.skipTest(), pytestmark = ...) belongs to the
                # test it sits in: the nearest def above with less indentation (Codex on #29).
                indent = len(code[line_start:m.start()]) - len(code[line_start:m.start()].lstrip())
                name = ""
                def_at = None
                for d in re.finditer(r"(?m)^([ \t]*)(?:async\s+)?def\s+(\w+)", code[:line_start]):
                    if len(d.group(1)) < indent:
                        name, def_at = d.group(2), d.start()
            if def_at is not None:
                # Qualify with the enclosing class: A.test_x and B.test_x are different tests
                # (Codex on #29).
                owner = _enclosing_class(code, def_at)
                if owner:
                    name = f"{owner}.{name}"
        else:
            # Every string between the marker and the test body or the end of its call: a
            # modifier's arguments (skipIf(cond)) and the test's name, not the body (Codex on #29).
            name = " ".join(re.findall(r'[0-9a-f]{8}"', _registration_args(after)))
            # Prefix the enclosing describe() path: describe("A") test("works") and describe("B")
            # test("works") are different tests (Codex on #29).
            suites = [n for start, end, n in describe_spans if start < m.start() < end]
            if suites:
                name = " > ".join(suites + [name])
        marks[f"{head} {name}"] += 1
    return marks


def warn_only_on_error(guard):
    """Before enforcement, a guard that can't inspect the PR (a symlinked manifest, an invalid
    fixture) warns like any other finding instead of failing (Copilot on #29)."""
    import functools

    @functools.wraps(guard)
    def wrapped(root, config, policy):
        try:
            return guard(root, config, policy)
        except KitError as exc:
            ctx = None
            try:
                ctx, _ = pull_request_context(policy)
            except KitError:
                pass
            if ctx and ctx["warn_only"]:
                return {"cases": 0, "note": "pull request opened before enforcement: warning only",
                        "warnings": [str(exc)]}
            raise
    return wrapped


@warn_only_on_error
def test_tampering(root, config, policy):
    """Fail a PR that deletes a test file, renames one away, lowers its assertion count or adds a skip.

    Survey issue #1 (ImpossibleBench: agents that cheat mostly edit the tests). Compares base and
    head with git only. Cleared by a decision id (DEC-n or DEC-YYYYMMDD-XXXX) or a
    `Tests-changed-because: <reason>` line in the PR body. Known limit: the check can't tell who
    wrote that line; owner review of the PR is what holds it to Zach's word.
    """
    ctx, skipped = pull_request_context(policy)
    if skipped:
        return skipped
    raw = git_out(root, "diff", "--name-status", "-M", "-z", ctx["base"], "HEAD").decode("utf-8", "replace").split("\0")
    findings = []
    i = 0
    while i < len(raw) and raw[i]:
        status = raw[i]
        if status[0] in "RC":
            old, new = raw[i + 1], raw[i + 2]
            i += 3
        else:
            old = new = raw[i + 1]
            i += 2
        if status[0] in "AC":
            # A new test file can't lose assertions, but a focused test in it (.only) can stop the
            # rest of the suite running, and a skip in it hides a test (Codex P1 on #29).
            if TEST_FILE.search(new):
                skips = tampering_counts(new, file_at(root, "HEAD", new) or "")[1]
                if skips:
                    findings.append(f"{new}: new test file with skips 0 -> {skips}")
            continue
        if not TEST_FILE.search(old):
            # A file renamed into a test name is new to the suite: check it like an added one
            # (Codex on #29).
            if status[0] == "R" and TEST_FILE.search(new):
                skips = tampering_counts(new, file_at(root, "HEAD", new) or "")[1]
                if skips:
                    findings.append(f"{new}: renamed into a test name with skips 0 -> {skips}")
            continue
        if status[0] == "D":
            findings.append(f"{old}: test file deleted")
            continue
        if status[0] == "R" and not TEST_FILE.search(new):
            findings.append(f"{old}: test file renamed to non-test {new}")
            continue
        base_text, head_text = file_at(root, ctx["base"], old) or "", file_at(root, "HEAD", new) or ""
        before, after = tampering_counts(old, base_text), tampering_counts(new, head_text)
        label = old if old == new else f"{old} -> {new}"
        if after[0] < before[0]:
            findings.append(f"{label}: assertions {before[0]} -> {after[0]}")
        if after[1] > before[1]:
            findings.append(f"{label}: skips {before[1]} -> {after[1]}")
        else:
            # Same count, different place: removing one skip mustn't cancel a new one elsewhere
            # (Copilot on #29). Compare the marker lines themselves.
            moved = marker_lines(new, head_text) - marker_lines(old, base_text)
            if moved:
                findings.append(f"{label}: {sum(moved.values())} new skip or focus marker(s) on lines that had none at base")
    cleared = DECISION_ID.search(ctx["body"]) or TESTS_CHANGED.search(ctx["body"])
    cases = sum(1 for p in tracked(root, "HEAD") if TEST_FILE.search(p))
    return guard_result(findings, ctx, cleared and cleared.group(0).strip(), cases,
                        "name a decision id (DEC-...) or add a 'Tests-changed-because: <reason>' line "
                        "to the PR body, then push again (a re-run reuses the old body)")


PEP508_NAME = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?:\[[^\]]*\])?\s*(@)?")
# A folder spec (./x, ../x, /x, ~/x) or a tarball file (x-1.0.0.tgz) is a package on the local
# filesystem (Codex on #29).
NPM_LOCAL = re.compile(r"^(?:file|link|workspace|portal|git|git\+\w+|https?|github|npm):|^[\w.-]+/[\w.-]+(?:#.*)?$|^(?:\.{1,2}|~)?/|^\.{1,2}$|\.(?:tgz|tar\.gz|tar)$")


def pypi_name(spec):
    """(normalised name, registry?) from a PEP 508 string; None when it names no package."""
    match = PEP508_NAME.match(spec)
    if not match:
        return None
    return re.sub(r"[-_.]+", "-", match.group(1)).lower(), not match.group(2)


def manifest_packages(path, text):
    """{(ecosystem, name): registry?} declared directly in one manifest. Lockfiles aren't read:
    their new entries are mostly transitive (the coordinator's addendum to the handoff)."""
    found = {}
    name = path.rsplit("/", 1)[-1]
    if name == "package.json":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise KitError(f"{path}: not valid JSON") from exc
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            section = data.get(key) if isinstance(data, dict) else None
            if isinstance(section, dict):
                for pkg, spec in section.items():
                    alias = re.match(r"npm:(@?[^@\s]+)", spec.strip()) if isinstance(spec, str) else None
                    if alias:
                        # An alias installs a registry package under another name: check the target
                        # (Codex on #29).
                        found[("npm", alias.group(1))] = True
                        continue
                    found[("npm", pkg)] = found.get(("npm", pkg), False) or not (isinstance(spec, str) and NPM_LOCAL.search(spec.strip()))
        return found
    specs, self_name = [], None
    if name == "pyproject.toml":
        import tomllib
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise KitError(f"{path}: not valid TOML") from exc
        project = data.get("project", {})
        self_name = project.get("name") or data.get("tool", {}).get("poetry", {}).get("name")
        specs += project.get("dependencies", []) or []
        build = data.get("build-system", {})
        specs += [r for r in (build.get("requires", []) if isinstance(build, dict) else []) or [] if isinstance(r, str)]
        for group in (project.get("optional-dependencies", {}) or {}).values():
            specs += group or []
        for group in (data.get("dependency-groups", {}) or {}).values():
            specs += [s for s in group or [] if isinstance(s, str)]
        poetry = data.get("tool", {}).get("poetry", {})
        tables = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
        tables += [g.get("dependencies", {}) for g in (poetry.get("group", {}) or {}).values() if isinstance(g, dict)]
        for table in tables:
            for pkg, spec in (table or {}).items():
                if pkg.lower() != "python":
                    local = isinstance(spec, dict) and any(k in spec for k in ("path", "git", "url"))
                    key = ("pypi", re.sub(r"[-_.]+", "-", pkg).lower())
                    found[key] = found.get(key, False) or not local
    else:   # requirements*.txt
        for line in text.splitlines():
            line = line.split(" #", 1)[0].strip()
            editable = re.match(r"(?:-e|--editable)(?:\s+|=)(\S+)", line)
            if editable:
                # A named editable install (-e git+...#egg=name) is a new non-registry package; an
                # unnamed local one (-e .) names nothing (Copilot on #29).
                egg = re.search(r"#egg=([A-Za-z0-9][A-Za-z0-9._-]*)", editable.group(1))
                if egg:
                    specs.append(f"{egg.group(1)} @ editable")
                continue
            archive = re.match(r"(?:\S*/)?([A-Za-z0-9][A-Za-z0-9._-]*?)-[0-9][^/\s;]*?\.(?:whl|tar\.gz|tar\.bz2|tgz|zip)\s*(?:;.*)?$", line)
            if archive and "://" not in line:
                # A local wheel or sdist (pkg-1.0-py3-none-any.whl, dist/pkg-1.0.tar.gz) is a new
                # non-registry package named by its file: it needs a decision id, but its file name
                # isn't looked up on PyPI (Codex on #29).
                specs.append(f"{archive.group(1)} @ local")
                continue
            if line and not line.startswith(("#", "-", ".", "/")) and (
                    "://" not in line or re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*\s*(?:\[[^\]]*\])?\s*@", line)):
                # A named direct reference (name @ https://...) is a new non-registry package: it
                # needs a decision id though nothing is looked up (Codex security review on #29).
                specs.append(line)
    own = re.sub(r"[-_.]+", "-", self_name).lower() if name == "pyproject.toml" and isinstance(self_name, str) else None
    for spec in specs:
        parsed = pypi_name(spec) if isinstance(spec, str) else None
        if parsed and parsed[0] == own:
            continue   # a self-referencing extra (all = ["mine[a]"]) is this project, not a registry package
        if parsed:
            # OR across declarations: a local one in another group can't mask a registry one (Codex on #29).
            found[("pypi", parsed[0])] = found.get(("pypi", parsed[0]), False) or (parsed[1] and "://" not in spec)
    if name == "pyproject.toml":
        # uv resolves a name through [tool.uv.sources] wherever it is declared: a path, git, url or
        # workspace source with no marker makes it local everywhere, so the registry lookup is skipped
        # (a decision id is still needed; a marker leaves other platforms on the registry). An `index`
        # source stays a registry package (Codex on #29).
        tool = data.get("tool", {}) if isinstance(data.get("tool"), dict) else {}
        uv = tool.get("uv", {}) if isinstance(tool.get("uv"), dict) else {}
        sources = uv.get("sources", {}) if isinstance(uv.get("sources"), dict) else {}
        for pkg, source in sources.items():
            entries = source if isinstance(source, list) else [source]
            key = ("pypi", re.sub(r"[-_.]+", "-", pkg).lower())
            if key in found and entries and all(isinstance(e, dict) and any(k in e for k in ("path", "git", "url", "workspace"))
                                                 and "marker" not in e for e in entries):
                found[key] = False
    return found


def is_manifest(path):
    name = path.rsplit("/", 1)[-1]
    if "node_modules/" in f"/{path}":
        return False
    return name in ("package.json", "pyproject.toml") or bool(re.fullmatch(r"requirements[^/]*\.txt", name))


def _workspace_glob(pattern):
    """A workspace glob as a regex over folder paths, segment by segment as npm reads it: * and ?
    stay inside one path segment, ** spans folders (Codex on #29)."""
    out, i = [], 0
    pattern = pattern.strip().rstrip("/")
    while pattern.startswith("./"):
        pattern = pattern[2:]
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _workspace_match(folder, spaces):
    """Is `folder` a workspace? Matched by a pattern and not excluded by a later !pattern."""
    hit = False
    for g in spaces:
        if not isinstance(g, str) or not g.strip():
            continue
        if g.startswith("!"):
            if hit and _workspace_glob(g[1:]).match(folder):
                hit = False
        elif _workspace_glob(g).match(folder):
            hit = True
    return hit


def workspace_names(root, rev):
    """npm package names defined by the root package.json's workspaces: a sibling is local, not a
    registry package. Only workspaces count, so a stray manifest elsewhere can't exempt a name
    (Codex on #29)."""
    root_text = file_at(root, rev, "package.json")
    try:
        spaces = json.loads(root_text).get("workspaces") if root_text else None
    except (ValueError, AttributeError):
        return set()
    if isinstance(spaces, dict):
        spaces = spaces.get("packages")
    if not isinstance(spaces, list):
        return set()
    names = {}
    for path in tracked(root, rev):
        folder = path.rsplit("/", 1)[0] if "/" in path else ""
        if path.endswith("/package.json") and _workspace_match(folder, spaces):
            try:
                data = json.loads(file_at(root, rev, path) or "")
                own, version = data.get("name"), data.get("version")
            except (ValueError, AttributeError):
                continue
            if isinstance(own, str):
                names[("npm", own)] = version if isinstance(version, str) else None
    return names


def _semver(text):
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", text.strip())
    return tuple(int(g) for g in m.groups()) if m else None


def _semver_satisfies(version, spec):
    """Does `version` satisfy an npm range? Covers ||, hyphen ranges, comparators, ^, ~ and x-ranges.
    None when the range isn't understood, so the caller keeps the safe reading (npm links a
    workspace only when its version satisfies the range: Codex on #29)."""
    if spec.strip() in ("", "*"):
        return True
    # Prereleases follow their own matching rules in npm (^1.0.0 does not take 1.0.0-beta.1): leave
    # them unknown so the registry checks stay (Codex on #29).
    if re.search(r"\d-[0-9A-Za-z]", version or "") or re.search(r"\d-[0-9A-Za-z]", spec):
        return None
    v = _semver(version or "")
    if v is None:
        return None

    def partial(text):
        m = re.fullmatch(r"v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?(?:[-+][\w.+-]*)?", text)
        if not m:
            return None
        return [None if g is None or g in "xX*" else int(g) for g in m.groups()]

    def bounds(op, text):
        """[(op, tuple)] comparators for one token."""
        p = partial(text)
        if p is None:
            raise ValueError(text)
        filled = [x if x is not None else 0 for x in p]
        n = next((i for i, x in enumerate(p) if x is None), 3)   # count of given parts
        lo = tuple(filled)
        def bump(i):
            t = filled[:i] + [0] * (3 - i)
            t[i - 1] += 1
            return tuple(t)
        if op in ("", "="):
            return [(">=", lo), ("<", bump(n))] if n < 3 else [("=", lo)] if n else []
        if op == "^":
            first = next((i for i, x in enumerate(filled[:max(n, 1)]) if x), max(n, 1) - 1)
            return [(">=", lo), ("<", bump(min(first + 1, n) if n else 1))] if n else []
        if op == "~":
            return [(">=", lo), ("<", bump(min(2, n) if n > 1 else 1))] if n else []
        if op == ">":
            return [(">=", bump(n))] if n < 3 else [(">", lo)]
        if op == "<=":
            return [("<", bump(n))] if n < 3 else [("<=", lo)]
        return [(op, lo)]   # >= and <

    def check(comps):
        ops = {"=": v.__eq__, ">=": v.__ge__, "<=": v.__le__, ">": v.__gt__, "<": v.__lt__}
        return all(ops[op](t) for op, t in comps)

    spec = spec.strip()
    if spec == "latest":
        return None
    try:
        for alt in spec.split("||"):
            alt = alt.strip()
            hyphen = re.fullmatch(r"(\S+)\s+-\s+(\S+)", alt)
            if hyphen:
                comps = bounds(">=", hyphen.group(1)) + bounds("<=", hyphen.group(2))
            else:
                comps = []
                for tok in re.findall(r"(>=|<=|>|<|=|\^|~>?)?\s*([^\s<>=^~]+)", alt):
                    comps += bounds(tok[0].replace("~>", "~"), tok[1])
            if not alt or check(comps):
                return True
        return False
    except ValueError:
        return None


def npm_registry_specs(text):
    """{name: [range]} for the registry declarations in one package.json (local ones such as
    workspace:* or file: are left out)."""
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    specs = {}
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        section = data.get(key) if isinstance(data, dict) else None
        if isinstance(section, dict):
            for pkg, spec in section.items():
                if isinstance(spec, str) and not NPM_LOCAL.search(spec.strip()):
                    specs.setdefault(pkg, []).append(spec)
    return specs


def repository_packages(root, rev, locations=None):
    """{key: registry?} across the repository's manifests; `locations`, when given, collects the
    manifest paths that declare each registry key."""
    packages = {}
    for path in tracked(root, rev):
        if is_manifest(path):
            for key, registry in manifest_packages(path, file_at(root, rev, path) or "").items():
                packages[key] = packages.get(key, False) or registry
                if locations is not None and registry:
                    locations.setdefault(key, set()).add(path)
    # pip follows -r/--requirement includes, whatever the included file is called: read them too,
    # recursively, within the repository's tracked files (Codex security review on #29).
    files = set(tracked(root, rev))
    # Each entry carries the top-level requirements file pip was pointed at: an --index-url there
    # applies to everything it includes, so included packages are located at that file for the
    # private-registry check (Codex on #29).
    queue = [(p, p) for p in files if re.fullmatch(r"requirements[^/]*\.txt", p.rsplit("/", 1)[-1])]
    seen = {p for p, _ in queue}
    already = {p for p in files if is_manifest(p)}
    while queue:
        path, top = queue.pop()
        text = file_at(root, rev, path) or ""
        if path not in already:
            for key, registry in manifest_packages("requirements.txt", text).items():
                packages[key] = packages.get(key, False) or registry
                if locations is not None and registry:
                    locations.setdefault(key, set()).add(top)
        folder = path.rsplit("/", 1)[0] if "/" in path else ""
        # pip accepts the short option attached (-rdeps.in) as well as -r deps.in and
        # --requirement=deps.in (Codex on #29).
        for inc in re.finditer(r"(?m)^\s*(?:-r\s*=?\s*|--requirement(?:\s+|\s*=\s*))(\S+)", text):
            target = posixpath.normpath(posixpath.join(folder, inc.group(1)))
            if target in files and target not in seen:
                seen.add(target)
                queue.append((target, top))
    # A sibling workspace is linked, not fetched, but only when its version satisfies every range
    # declared for it; otherwise npm goes to the registry (Codex on #29).
    own = workspace_names(root, rev)
    ranges = {}
    for path in tracked(root, rev):
        if path.rsplit("/", 1)[-1] == "package.json" and own:
            for pkg, specs in npm_registry_specs(file_at(root, rev, path) or "").items():
                ranges.setdefault(("npm", pkg), []).extend(specs)
    return {key: registry for key, registry in packages.items()
            if key not in own or any(_semver_satisfies(own[key], r) is not True for r in ranges.get(key, []))}



PUBLIC_REGISTRY_HOSTS = {"registry.npmjs.org", "registry.yarnpkg.com", "pypi.org", "pypi.python.org",
                         "files.pythonhosted.org"}


def is_private_url(url):
    from urllib.parse import urlparse
    host = (urlparse(url.strip().strip("\"'")).hostname or "").lower()
    return bool(host) and host not in PUBLIC_REGISTRY_HOSTS


def private_registries(root):
    """Where this repository resolves through a registry other than the public one, as
    {(token, scope)}: token is "npm", "npm:@scope" or "pypi"; scope is the folder a config file
    governs ("dir/") or the one file it governs. A public 404 there proves nothing, so it's
    reported as unconfirmed (Codex on #29). Only a configured URL whose host isn't the public
    registry counts (Codex on #29), and only for the manifests that config applies to (Copilot on
    #29): a project .npmrc covers its own folder (the root one covers every workspace), a pyproject
    or uv.toml index covers its own folder, a requirements index-url covers that file."""
    import tomllib
    private = set()
    files = set(tracked(root, "HEAD"))
    for path in sorted(files):
        name = path.rsplit("/", 1)[-1]
        folder = path[: -len(name)]
        if name == ".npmrc":
            for line in (file_at(root, "HEAD", path) or "").splitlines():
                key, _, value = line.partition("=")
                key = key.strip()
                if key == "registry" and is_private_url(value):
                    private.add(("npm", folder))
                elif key.startswith("@") and key.endswith(":registry") and is_private_url(value):
                    private.add(("npm:" + key[: -len(":registry")], folder))
        elif name in ("pyproject.toml", "uv.toml"):
            try:
                data = tomllib.loads(file_at(root, "HEAD", path) or "")
            except tomllib.TOMLDecodeError:
                continue
            tool = data.get("tool", {}) if isinstance(data.get("tool"), dict) else {}
            entries = [data.get("index"), tool.get("uv", {}).get("index") if isinstance(tool.get("uv"), dict) else None,
                       tool.get("poetry", {}).get("source") if isinstance(tool.get("poetry"), dict) else None,
                       tool.get("pdm", {}).get("source") if isinstance(tool.get("pdm"), dict) else None]
            for group in entries:
                for entry in group if isinstance(group, list) else []:
                    if isinstance(entry, dict) and isinstance(entry.get("url"), str) and is_private_url(entry["url"]):
                        private.add(("pypi", folder))
        elif is_manifest(path) and name != "package.json":
            # pip applies an index option from any file in a -r chain to the whole run, so a
            # requirements file is private if it or anything it includes sets a private index
            # (Codex on #29).
            chain = [path] + (requirements_includes(root, "HEAD", path, files) if name.startswith("requirements") else [])
            for member in chain:
                for match in re.finditer(r"(?m)^\s*(?:--(?:extra-)?index-url|-i|(?:extra-)?index-url)\s*[=\s]\s*(\S+)",
                                         file_at(root, "HEAD", member) or ""):
                    if is_private_url(match.group(1)):
                        private.add(("pypi", path))
    return private


def requirements_includes(root, rev, top, files):
    """Every tracked file reached from `top` through -r/--requirement includes, once each."""
    out, queue, seen = [], [top], {top}
    while queue:
        path = queue.pop()
        folder = path.rsplit("/", 1)[0] if "/" in path else ""
        for inc in re.finditer(r"(?m)^\s*(?:-r\s*=?\s*|--requirement(?:\s+|\s*=\s*))(\S+)", file_at(root, rev, path) or ""):
            target = posixpath.normpath(posixpath.join(folder, inc.group(1)))
            if target in files and target not in seen:
                seen.add(target)
                out.append(target)
                queue.append(target)
    return out


def privately_resolved(token, manifests, private):
    """True when every manifest declaring the package is governed by a private config for token."""
    def covered(manifest):
        folder = manifest[: len(manifest) - len(manifest.rsplit("/", 1)[-1])]
        return any(t == token and (scope == manifest or scope == folder or (t.startswith("npm") and scope == ""))
                   for t, scope in private)
    return bool(manifests) and all(covered(m) for m in manifests)


def registry_status(url):
    """HTTP status of a registry lookup, or None when the registry couldn't be reached. Standard library only."""
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url, headers={"User-Agent": "lydian-new-dependencies"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError, ValueError):
        return None


def registry_url(ecosystem, name):
    from urllib.parse import quote
    if ecosystem == "pypi":
        return f"https://pypi.org/pypi/{quote(name, safe='')}/json"
    return f"https://registry.npmjs.org/{quote(name, safe='@')}"


@warn_only_on_error
def new_dependencies(root, config, policy):
    """Fail a PR that adds a package without naming a decision, or adds one that doesn't exist.

    Survey issue #5 (about 20% of model-suggested packages don't exist; Spracklen et al.). Reads
    package.json, pyproject.toml and requirements*.txt at base and head, anywhere in the
    repository, and compares the packages each ecosystem declares directly: a version bump or a
    package moved between manifests isn't new. Each new registry package is looked up on PyPI or
    npm (the only network this kit makes); a 404 fails even with a decision id, and an unreachable
    registry is a warning, not a failure.
    """
    ctx, skipped = pull_request_context(policy)
    if skipped:
        return skipped
    before = repository_packages(root, ctx["base"])
    where = {}
    after = repository_packages(root, "HEAD", where)
    # New, or switched from a local source (file:, path, git) to the registry: either way a registry
    # package arrives (Codex security review on #29).
    added = sorted(key for key in after if key not in before or (after[key] and not before[key]))
    private = private_registries(root) if added else set()
    findings, warnings = [], []
    for ecosystem, name in added:
        if not after[(ecosystem, name)]:
            continue
        status = registry_status(registry_url(ecosystem, name))
        scope = f"npm:{name.split('/')[0]}" if name.startswith("@") else None
        manifests = where.get((ecosystem, name), set())
        if status == 404 and (privately_resolved(ecosystem, manifests, private)
                              or (scope and privately_resolved(scope, manifests, private))):
            warnings.append(f"{ecosystem}:{name}: not on the public registry; this repository uses a private one, so unconfirmed")
        elif status == 404:
            findings.append(f"{ecosystem}:{name}: package does not exist (possibly invented)")
        elif status != 200:
            warnings.append(f"{ecosystem}:{name}: could not reach the registry to confirm it exists ({status})")
    decision = DECISION_ID.search(ctx["body"])
    if added and not decision:
        findings.insert(0, "new dependencies with no decision id in the PR body: "
                        + ", ".join(f"{e}:{n}" for e, n in added))
    invented = [f for f in findings if "possibly invented" in f]
    if invented and not ctx["warn_only"]:
        raise KitError("; ".join(findings))   # a decision id doesn't make a package exist
    result = guard_result(findings, ctx, decision and decision.group(0), len(added),
                          "name the decision that approved them (DEC-...) in the PR body, then push again")
    if warnings:
        result["warnings"] = result.get("warnings", []) + warnings
    return result


MODULES = {"interoperability": interoperability, "modularity": modularity,
           "repository-tests": repository_tests, "repository-commands": repository_commands,
           "hook-guard": hook_guard, "telosmith": telosmith,
           "test-tampering": test_tampering, "new-dependencies": new_dependencies}


def flag(policy, key):
    """True only when a module's policy is an object and sets `key` to exactly true (not 1 or "yes")."""
    return isinstance(policy, dict) and policy.get(key) is True


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
    ordered = sorted(definitions.items(), key=lambda kv: not flag(kv[1], "run_first"))
    # A run_first module is a security preflight: if it doesn't pass, nothing after it runs.
    # The checkout it rejected must never reach repository-controlled code (Abigail's probe of
    # 0743ae2: hook-guard failed and repository-tests still ran). Later modules are reported as
    # not-run, with the preflight named, so the report shows why.
    preflight_failed = None
    for name, policy in ordered:
        if preflight_failed:
            results.append({"module": name, "status": "not-run", "cases": 0,
                            "reason": f"not run: security preflight {preflight_failed!r} did not pass"})
            continue
        result = {"module": name, "status": "failed", "cases": 0}
        try:
            if name not in MODULES:
                result["status"] = "blocked"
                raise KitError("central suite names an unknown module")
            if name not in config and flag(policy, "optional"):
                result["status"] = "not-declared"
                results.append(result)
                continue
            if name not in config and flag(policy, "adapter_optional"):
                config = dict(config, **{name: {}})
            if name not in config:
                result["status"] = "blocked"
                raise KitError("required module lacks an adapter")
            if not isinstance(config[name], dict) or not isinstance(policy, dict):
                raise KitError("module configuration and policy must be objects")
            outcome = MODULES[name](root, config[name], policy)
            # A module returns its case count, or {"cases", "note"?, "warnings"?}: a warning is
            # reported and never fails the gate (the PR guards' warn-only rollout).
            if isinstance(outcome, dict):
                result.update({k: outcome[k] for k in ("note", "warnings") if outcome.get(k)})
                outcome = outcome.get("cases", 0)
            result["cases"] = outcome
            result["status"] = "passed"
        except (KitError, KeyError, ValueError, TypeError, SyntaxError, OSError) as exc:
            # Do not echo source snippets, process output, or secrets into a shared report.
            result["reason"] = str(exc) if isinstance(exc, KitError) else type(exc).__name__
        if flag(policy, "run_first") and result["status"] != "passed":
            preflight_failed = name
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
