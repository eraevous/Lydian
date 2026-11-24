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
TEST_FILE = re.compile(r"(?:^|/)(?:test_[^/]*\.py|[^/]*_test\.py|tests\.py|[^/]*\.(?:test|spec)\.[cm]?[jt]sx?)$")
PY_ASSERTS = re.compile(r"(?<![\w.])assert\b|\.assert[A-Z_]\w*\s*\(")
PY_SKIPS = re.compile(r"\bmark\.(?:skip|skipif|xfail)\b|\bunittest\.(?:skip|skipIf|skipUnless|expectedFailure)\b"
                      r"|\bpytest\.(?:skip|xfail)\s*\(|\.skipTest\s*\(")
JS_ASSERTS = re.compile(r"(?<![\w.$])expect\s*\(|(?<![\w.$])assert\b")
JS_SKIPS = re.compile(r"\.(?:skip|skipIf|only)\s*\(|(?<![\w.$])x(?:it|test|describe)\s*\(|\.todo\s*\(")
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


def tampering_counts(path, text):
    """(assertions, skips) in one test file, by language. Comments and strings count too: it's a diff signal."""
    # Line comments don't count, so "assert True  # assert f() == 2" is one assertion, not two.
    # A focused test (.only) disables its siblings, so it counts as a skip.
    if path.endswith(".py"):
        text = re.sub(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'', "", text)   # docstrings and string blocks
        text = re.sub(r"#[^\n]*", "", text)
        return len(PY_ASSERTS.findall(text)), len(PY_SKIPS.findall(text))
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)   # block comments (Codex on #29)
    text = re.sub(r"(?<![:\\])//[^\n]*", "", text)
    return len(JS_ASSERTS.findall(text)), len(JS_SKIPS.findall(text))


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
            continue
        if status[0] == "D":
            findings.append(f"{old}: test file deleted")
            continue
        if status[0] == "R" and not TEST_FILE.search(new):
            findings.append(f"{old}: test file renamed to non-test {new}")
            continue
        before = tampering_counts(old, file_at(root, ctx["base"], old) or "")
        after = tampering_counts(new, file_at(root, "HEAD", new) or "")
        label = old if old == new else f"{old} -> {new}"
        if after[0] < before[0]:
            findings.append(f"{label}: assertions {before[0]} -> {after[0]}")
        if after[1] > before[1]:
            findings.append(f"{label}: skips {before[1]} -> {after[1]}")
    cleared = DECISION_ID.search(ctx["body"]) or TESTS_CHANGED.search(ctx["body"])
    cases = sum(1 for p in tracked(root, "HEAD") if TEST_FILE.search(p))
    return guard_result(findings, ctx, cleared and cleared.group(0).strip(), cases,
                        "name a decision id (DEC-...) or add a 'Tests-changed-because: <reason>' line "
                        "to the PR body, then push again (a re-run reuses the old body)")


PEP508_NAME = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?:\[[^\]]*\])?\s*(@)?")
NPM_LOCAL = re.compile(r"^(?:file|link|workspace|portal|git|git\+\w+|https?|github|npm):|^[\w.-]+/[\w.-]+(?:#.*)?$")


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
                    found[("npm", pkg)] = not (isinstance(spec, str) and NPM_LOCAL.search(spec.strip()))
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
                    found[("pypi", re.sub(r"[-_.]+", "-", pkg).lower())] = not local
    else:   # requirements*.txt
        for line in text.splitlines():
            line = line.split(" #", 1)[0].strip()
            if line and not line.startswith(("#", "-", ".", "/")) and "://" not in line:
                specs.append(line)
    own = re.sub(r"[-_.]+", "-", self_name).lower() if name == "pyproject.toml" and isinstance(self_name, str) else None
    for spec in specs:
        parsed = pypi_name(spec) if isinstance(spec, str) else None
        if parsed and parsed[0] == own:
            continue   # a self-referencing extra (all = ["mine[a]"]) is this project, not a registry package
        if parsed:
            found[("pypi", parsed[0])] = parsed[1] and "://" not in spec
    return found


def is_manifest(path):
    name = path.rsplit("/", 1)[-1]
    if "node_modules/" in f"/{path}":
        return False
    return name in ("package.json", "pyproject.toml") or bool(re.fullmatch(r"requirements[^/]*\.txt", name))


def workspace_names(root, rev):
    """npm package names defined by the root package.json's workspaces: a sibling is local, not a
    registry package. Only workspaces count, so a stray manifest elsewhere can't exempt a name
    (Codex on #29)."""
    import fnmatch
    root_text = file_at(root, rev, "package.json")
    try:
        spaces = json.loads(root_text).get("workspaces") if root_text else None
    except (ValueError, AttributeError):
        return set()
    if isinstance(spaces, dict):
        spaces = spaces.get("packages")
    if not isinstance(spaces, list):
        return set()
    names = set()
    for path in tracked(root, rev):
        folder = path.rsplit("/", 1)[0] if "/" in path else ""
        if path.endswith("/package.json") and any(isinstance(g, str) and fnmatch.fnmatch(folder, g.rstrip("/")) for g in spaces):
            try:
                own = json.loads(file_at(root, rev, path) or "").get("name")
            except (ValueError, AttributeError):
                continue
            if isinstance(own, str):
                names.add(("npm", own))
    return names


def repository_packages(root, rev):
    packages = {}
    for path in tracked(root, rev):
        if is_manifest(path):
            for key, registry in manifest_packages(path, file_at(root, rev, path) or "").items():
                packages[key] = packages.get(key, False) or registry
    own = workspace_names(root, rev)
    return {key: registry for key, registry in packages.items() if key not in own}


def private_registries(root):
    """Ecosystems (or npm scopes) this repository resolves through a registry other than the public
    one. A public 404 there proves nothing, so it's reported as unconfirmed (Codex on #29)."""
    private = set()
    for path in tracked(root, "HEAD"):
        name = path.rsplit("/", 1)[-1]
        text = None
        if name == ".npmrc":
            text = file_at(root, "HEAD", path) or ""
            for line in text.splitlines():
                key = line.split("=", 1)[0].strip()
                if key == "registry":
                    private.add("npm")
                elif key.startswith("@") and key.endswith(":registry"):
                    private.add("npm:" + key[: -len(":registry")])
        elif name in ("pip.conf", "pip.ini", "uv.toml", "poetry.toml") or (
                is_manifest(path) and name != "package.json"):
            text = file_at(root, "HEAD", path) or ""
            if re.search(r"(?m)^\s*(?:--(?:extra-)?index-url|-i\s|index-url\s*=|\[\[tool\.(?:uv\.index|poetry\.source|pdm\.source)\]\]|\[\[index\]\])", text):
                private.add("pypi")
    return private


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
    after = repository_packages(root, "HEAD")
    added = sorted(key for key in after if key not in before)
    private = private_registries(root) if added else set()
    findings, warnings = [], []
    for ecosystem, name in added:
        if not after[(ecosystem, name)]:
            continue
        status = registry_status(registry_url(ecosystem, name))
        scope = f"npm:{name.split('/')[0]}" if name.startswith("@") else None
        if status == 404 and (ecosystem in private or scope in private):
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
