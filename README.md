# Lydian

The shared touchstone for Kairos: one test kit and CI that every repo runs, so a check changes once and applies everywhere.

Named for the Lydian stone, the touchstone that showed whether gold was real.

## What it is for

Kairos has many repositories. Without a shared kit, adding, removing or fixing a test means editing every repository by hand. Lydian keeps the tests, fixtures and policies in one place. Each repository keeps only:

- a ~10-line workflow that calls Lydian's (`examples/caller/kairos.yml`), which never changes when tests change;
- a small adapter file (`.kairos/adapter.json`) that says how that repository differs: paths, its own test folder, which checks apply.

## Suites

- `kairos-v1` (default, what repositories run): `repository-tests`, `telosmith`, and `repository-commands` when the repository declares any.
- `kairos-records-v1`: `telosmith`, plus `repository-commands` when declared, for repositories with no unittest suite Lydian can run yet (no Python tests, or pytest/third-party dependencies). Their own CI keeps running their tests.
- `component-v1`: a demo profile that exercises all four modules against `examples/component_repo`.

## Checks (modules)

| Module | What it checks |
| --- | --- |
| `repository-tests` | Runs the repository's own unittest suite. Zero tests, all-skipped, failures and unexpected skips fail. |
| `repository-commands` | Runs the test commands the repository declares in its adapter (`{"commands": [{"name": ..., "argv": [...]}]}`): suites unittest can't discover, such as a custom runner, node tests or a scanner. Only programs the central suite allows (`{python}`, `node`) may start a command. Each passes on exit 0; output is never copied into the report. Optional: a repository that declares none shows `not-declared`. A gate still needs at least one module that passed. |
| `hook-guard` | Runs in every repository, with no adapter entry. Each agent hook command in `.claude/settings.json` (and in a committed `settings.local.json`) must match a SHA-256 fingerprint listed in the suite: under `*` for every repository, or under the repository's name. A new or changed hook fails until its fingerprint is added here in Lydian, so a branch can't quietly change what runs on the next person's machine. Every other setting in those files (for example `env`, which could redirect an approved hook) and a root `.mcp.json` need fingerprints too, and unknown fields in hook entries fail. It runs before any repository code. It also fails on a root module file, compiled file or package folder that would shadow a module a hook imports (`aec`, `runpy`, `site`, `sitecustomize`, …). It can't vet code a hook legitimately runs from the checkout, and it can't stop a PR that rewrites the workflow calling it. Owner review of `.github/` and `.claude/` changes covers those. |
| `telosmith` | Runs Telosmith's structural edges snapshot and records check on the repository's `.aec/records.jsonl`. Telosmith is pinned here, not in each repository. Missing or malformed records fail. |
| `modularity` | Declared forbidden absolute Python imports, by static parsing (no candidate code is imported). |
| `interoperability` | Producer and consumer commands against central wire fixtures, including explicit rejection of invalid input. |

Every check has a case it must flag and a clean decoy it must not (see `tests/`).

## How a repository uses it

1. Copy `examples/caller/kairos.yml` to `.github/workflows/kairos.yml`.
2. Add `.kairos/adapter.json` (copy `examples/consumer_repo/.kairos/adapter.json`, profile `kairos-v1`), and keep at least one record in `.aec/records.jsonl`.
3. Require the single check `checks / kairos-checks` in the repository's ruleset.

The shared workflow runs with a read-only token, and its checkouts keep no credentials.

## Versions

Callers use `@main` (DEC-210). A merged change reaches every repository on its next run; there is no tag to move and no per-repository pull request.

- **Trust:** callers follow mutable code. The guard is this repository's protected `main`, its own tests, and review before merge. A green check in a consumer is not proof the shared workflow is safe.
- **Rollback:** revert the change on Lydian `main`, or pin one caller to a full commit SHA while it's fixed.
- **Breaking changes:** if one is ever needed, it gets a new suite name (for example `kairos-v2`), so existing callers keep working.

## Status

- Initial import of the prototype (local, unpublished until now). Not yet called by any repository.
- First consumers: Telosmith and MtG, before the rest.
- Not a sandbox outside GitHub Actions: locally, run it only on reviewed code.
- Earlier design notes: `docs/prototype-notes.md`.

## Running the kit's tests

```sh
KAIROS_TELOSMITH_SRC=/path/to/Telosmith/src python3 -I -B -m unittest discover -s tests -v
```

## License

MIT (see [LICENSE](LICENSE)). Contributions are under the [CLA](CLA.md).
