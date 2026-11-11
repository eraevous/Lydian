# Lydian

The shared touchstone for Kairos: one test kit and CI that every repo runs, so a check changes once and applies everywhere.

Named for the Lydian stone, the touchstone that showed whether gold was real.

## What it is for

Kairos has many repositories. Without a shared kit, adding, removing or fixing a test means editing every repository by hand. Lydian keeps the tests, fixtures and policies in one place. Each repository keeps only:

- a ~10-line workflow that calls Lydian's (`examples/caller/kairos.yml`), which never changes when tests change;
- a small adapter file (`.kairos/adapter.json`) that says how that repository differs: paths, its own test folder, which checks apply.

## Suites

- `kairos-v1` (default, what repositories run): `repository-tests` and `telosmith`.
- `component-v1`: a demo profile that exercises all four modules against `examples/component_repo`.

## Checks (modules)

| Module | What it checks |
| --- | --- |
| `repository-tests` | Runs the repository's own unittest suite. Zero tests, all-skipped, failures and unexpected skips fail. |
| `telosmith` | Runs Telosmith's structural edges snapshot and records check on the repository's `.aec/records.jsonl`. Telosmith is pinned here, not in each repository. Missing or malformed records fail. |
| `modularity` | Declared forbidden absolute Python imports, by static parsing (no candidate code is imported). |
| `interoperability` | Producer and consumer commands against central wire fixtures, including explicit rejection of invalid input. |

Every check has a case it must flag and a clean decoy it must not (see `tests/`).

## How a repository uses it

1. Copy `examples/caller/kairos.yml` to `.github/workflows/kairos.yml`.
2. Add `.kairos/adapter.json` (see `examples/component_repo/adapter.json`).
3. Require the single check `checks / kairos-checks` in the repository's ruleset.

The shared workflow runs with a read-only token, and its checkouts keep no credentials.

## Versions

Callers use `@v1`. A change reaches every repository on its next run, with no per-repository pull requests. Before the `v1` tag moves, the change passes Lydian's own tests and review here.

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
