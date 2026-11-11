# Shared Kairos test kit: local prototype

This is a bounded, unpublished prototype. It is not installed in any repository's
CI, and no ruleset, workflow, repository setting, branch or remote file was changed.
Hosting remains undecided. `kairos-testkit` is a proposed separate developer-tool
repository, not an existing repository or an approved public release.

## What works

- Test implementations and profiles live centrally, not in consumer repositories.
  A small consumer adapter declares paths, component boundaries and entry points.
- Central interoperability fixtures test producer output, consumer output and
  explicit rejection of invalid wire formats. The demonstration wire format is
  synthetic. It is not represented as a Kairos product contract.
- A narrow static module checks declared forbidden **absolute Python imports**.
  It does not import candidate code. Strings and similar-looking module names
  are clean decoys. Missing files and syntax errors fail.
- The unittest adapter rejects zero tests, all-skipped runs, failures and
  unexpected skips. If a central profile explicitly allows skips, at least one
  complete passing test method is still required. Counts are successful methods,
  not assertions or subtests. Test diagnostics do not corrupt the JSON report.
- A required missing adapter, unknown module, empty suite or timeout cannot
  produce a passing gate. Independent modules still report after another fails.
- Regression tests prove that editing one central wire fixture makes two
  unchanged synthetic adapters fail, then restores both when reverted.
- A separate real compatibility pilot runs Telosmith at a specified full SHA
  against MtG at a specified full SHA. It requires clean checkouts before and
  after, regenerates structural edges into a temporary directory, validates
  records, distinguishes the existing authorized/unauthorized scenario pair,
  and verifies rejection of a malformed record. It does not update MtG's pin.

## Run

From the parent directory:

```sh
python3 -I -B -m unittest discover -s shared-testkit-prototype/tests -v
python3 -I -B shared-testkit-prototype/kit/run.py \
  --repo shared-testkit-prototype/examples/component_repo \
  --adapter shared-testkit-prototype/examples/component_repo/adapter.json \
  --suite shared-testkit-prototype/suites/component-v1.json
```

Real compatibility pilot (existing fresh clones, no network needed):

```sh
python3 -B shared-testkit-prototype/kit/pilot_telosmith.py \
  --tool ci-standardize-telosmith \
  --tool-sha f4f16d3dc5ed9cc6f6b01d778c8bbca03eaab8ca \
  --consumer ci-standardize-mtg \
  --consumer-sha acf96e072ff0524b7fecbf8bf9fa74e9f9258e13
```

## What this does not establish

- No hosted Actions run or merge gate exists for this kit. The proposed stable
  gate name is `kairos-checks`. Existing `aec` requirements remain unchanged and
  the nonexistent-check mismatch remains unresolved until an integration is
  reviewed and the actual check contexts are verified.
- No general modularity, architectural quality or semantic correctness proof.
  Relative imports, dynamic imports, cycles, package installation, dependency
  versions and non-Python boundaries are not covered by this prototype.
- No claim that all product tests pass. Telosmith's Python matrix and MtG's
  domain, drift, grammar and database checks remain separate and are not replaced.
- No claim that successful structural records establish true purpose, real
  authority, or improved Telosmith quality. The pilot's scenario assertions are
  narrow marker checks, not a full semantic oracle.
- The subprocess environment omits common credential variables, but this is
  **not a sandbox**. Candidate code could read files, spawn children or access the
  network. Use only reviewed local code for this prototype. Production needs
  disposable execution without secrets/write tokens, process-group limits and
  bounded output. The current timeout does not guarantee killing descendants.
- Reports are not signed attestations. A consumer-controlled manifest can weaken
  its own declared boundary, and candidate code could spoof a test summary.
  Production needs trusted baseline capability policy and reduction review.
- No fleet publisher, scheduled runs, auto-update bot or release machinery was
  provisioned. No new persistent account access or permissions were requested.

## Central changes without hand-editing every repository

1. Edit a module, shared fixture or profile once in the kit.
2. Run the candidate kit against pinned revisions of enrolled consumers. The
   central fleet can adopt new tests immediately; it records which suite ran.
3. Release an immutable revision after review.
4. An update bot can propose consumer Action-SHA changes as small PRs, rerunning
   existing tests before adoption. This is proposed, not built or authorized.

There is no honest way for a fixed SHA pin to silently run new code. Consumers
adopt new shared tests when the pin-update PR merges; automatic central testing
does not itself update their merge gates. Avoid moving `@main`/`@v1` references
or mutable downloaded profiles disguised as immutable dependencies. True
zero-consumer-update check publishing needs a separately authorized service
or organization policy and is outside this prototype.

## Placement and migration

A separate shared developer-test repository is the recommended home. KAIROS's
current operating rules limit it to the index/site/installer. QAT-Kits serves
conversational tools, and Telosmith and relay-kit have different responsibilities.

Start additively with Telosmith and MtG. Retain all existing domain tests and the
MtG security PR's credential-isolation work. Before proposing a common required
gate, verify exact emitted contexts, fail/cancel/skip behavior, matrix completion,
and successful hosted runs. Security-sensitive ruleset changes remain a separate
explicit approval, never a side effect of adding this kit.

## Coordinator additions (2026-11-10, local, unpublished)

- **`telosmith` module** (`kit/run.py`, tests in `tests/test_telosmith_module.py`, 4 tests).
  - The kit runs Telosmith's edges snapshot and records check against the consumer's `.aec/records.jsonl`.
  - Telosmith is pinned by the kit, not by each repo, so no repo copies CI snippets again. This is the gap that left Anamnesis's `aec` check never running.
  - Fails closed: missing records, malformed records, or no Telosmith source given to the kit all fail.
  - Run its tests with `KAIROS_TELOSMITH_SRC=<telosmith>/src`. Without it they skip.
- **Reusable workflow** `.github/workflows/kairos-checks.yml`.
  - Every repo calls it from one ~10-line file, `examples/caller/kairos.yml`, that never changes when tests change.
  - It uses a read-only token, and the consumer and kit checkouts don't keep credentials.
  - The check appears as `checks / kairos-checks`. That is the one context every ruleset would require (verify on the first hosted run).
- **The one owner decision this leaves:** pin `@v1` or a full SHA.
  - `@v1` (a moving tag): a kit change reaches every repo at once with zero per-repo PRs. That's Zach's stated goal. The kit repo's own ruleset and tests are what guard it.
  - Full SHA: each repo adopts kit changes through a small auto-PR. Slower, but every repo's gate only changes when that repo merges.
- `Lydian` is a placeholder until the repo is named.
