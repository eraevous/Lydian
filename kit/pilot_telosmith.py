"""Read-only, exact-revision Telosmith/consumer compatibility pilot. Not a sandbox."""
import argparse
import json
from pathlib import Path
import re
import sys
import tempfile

from run import command, KitError, within

SCENARIOS = {
    "dio_v312_authorized.jsonl": ("[rebaseline]", "[stale]"),
    "dio_v312_unauthorized.jsonl": ("[stale]", "[rebaseline]"),
}


def exact_checkout(path, expected):
    if not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise KitError("expected revision must be a full commit SHA")
    head = command(path, ["git", "rev-parse", "HEAD"])
    status = command(path, ["git", "status", "--porcelain", "--ignored"])
    if head.returncode or status.returncode or head.stdout.strip() != expected or status.stdout.strip():
        raise KitError("checkout is dirty or does not match the declared revision")


def pilot(tool, tool_sha, consumer, consumer_sha):
    tool, consumer = Path(tool).resolve(), Path(consumer).resolve()
    exact_checkout(tool, tool_sha)
    exact_checkout(consumer, consumer_sha)
    src = within(tool, "src")
    records = within(consumer, ".aec/records.jsonl")
    scenario_dir = within(consumer, ".aec/scenarios")
    if not records.is_file():
        raise KitError("consumer has no records")
    scenarios = sorted(scenario_dir.glob("*.jsonl"))
    if not set(SCENARIOS).issubset({s.name for s in scenarios}):
        raise KitError("MtG pilot lacks its required authorized/unauthorized scenario pair")
    prefix = ["{python}", "-I", "-B", "-c",
              "import sys, runpy; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module('aec', run_name='__main__')",
              str(src)]
    results = []
    with tempfile.TemporaryDirectory(prefix="kit-pilot-") as tmp:
        edges = str(Path(tmp) / "edges.jsonl")
        jobs = [("regenerate structural edges", ["edges", ".", "-o", edges]),
                ("validate consumer records", ["check", str(records), "--edges", edges])]
        jobs.extend(("scenario " + s.name, ["impact", str(records), str(within(consumer, str(s.relative_to(consumer))))])
                    for s in scenarios)
        for name, argv in jobs:
            result = command(consumer, prefix + argv, timeout=120)
            ok = result.returncode == 0
            scenario = name.removeprefix("scenario ")
            if scenario in SCENARIOS:
                expected, forbidden = SCENARIOS[scenario]
                ok = ok and expected in result.stdout and forbidden not in result.stdout
            results.append({"check": name, "status": "passed" if ok else "failed",
                            "exit_code": result.returncode,
                            "output": (result.stdout + result.stderr).strip()})
        malformed = Path(tmp) / "malformed.jsonl"
        malformed.write_text('{}\n', encoding="utf-8")
        result = command(consumer, prefix + ["check", str(malformed)], timeout=120)
        diagnostic = result.stdout + result.stderr
        rejected = (result.returncode == 1 and "[ERROR] version:" in diagnostic
                    and re.search(r"aec check: [1-9][0-9]* error\(s\).*\(structural\)", diagnostic) is not None)
        results.append({"check": "reject malformed record fixture",
                        "status": "passed" if rejected else "failed",
                        "exit_code": result.returncode, "expected_exit_code": 1})
    exact_checkout(tool, tool_sha)
    exact_checkout(consumer, consumer_sha)
    return {"schema": "kairos-testkit/compatibility-pilot@1", "scope": "local-structural-compatibility",
            "tool_sha": tool_sha, "consumer_sha": consumer_sha,
            "gate": "passed" if all(x["status"] == "passed" for x in results) else "failed",
            "checks": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--tool-sha", required=True)
    parser.add_argument("--consumer", required=True)
    parser.add_argument("--consumer-sha", required=True)
    args = parser.parse_args()
    try:
        report = pilot(args.tool, args.tool_sha, args.consumer, args.consumer_sha)
    except (KitError, OSError, ValueError) as exc:
        report = {"gate": "failed", "reason": str(exc)}
    print(json.dumps(report, indent=2))
    return 0 if report["gate"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
