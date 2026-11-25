# Adding a hook fingerprint to hook-guard's allowlist

`hook-guard` (see the README's module table) fails any agent hook command whose fingerprint (a SHA-256 content hash of the command text) isn't listed in the suite: under `"*"` for every repository, or under one repository's name. Adding a fingerprint widens what may run on the machine of anyone who opens an agent session in a Kairos repository, so every addition follows the steps below. Any lane (an agent) may follow them; only a merge by Zach (Zach Rhodes, the owner) approves one.

Written for handoff (a delegated task record) HO-LYDIAN-20261122-228P (Zach, 2026-11-22: "yes, with a protocol for doing so"; DEC-263 clause 5, an entry in the Kairos decision ledger, which works like Architecture Decision Records). Earlier additions (#9, #10, #13, #20) were made without a written protocol; their records are in the table at the end.

## Steps

### 1. Recompute

Compute the fingerprint yourself, with hook-guard's own rule, from the `.claude/settings.json` at the head commit of the PR that needs it:

```sh
git show <head sha>:.claude/settings.json | python3 -c '
import hashlib, json, sys
s = json.load(sys.stdin)
for event, matchers in s.get("hooks", {}).items():
    for m in matchers:
        for h in m["hooks"]:
            print(hashlib.sha256(h["command"].encode("utf-8")).hexdigest())'
```

This is the same digest `guard_fingerprints` in `kit/run.py` takes (SHA-256 of the command string's UTF-8 bytes). Never copy a fingerprint from a log, a CI message or a PR body: those are evidence of which hook failed, not of what the hook is. Check that your digest starts with the 12 characters the CI message printed. If the PR's head has moved since that run, use the new head and say so.

### 2. Trace the hook to its source

Show that the hook text is byte-identical to what the source it claims to come from produces. For a Telosmith hook:

```sh
git -C <Telosmith clone> worktree add /tmp/ts <commit in the repo's tools/aec_upstream.json>
cd "$(mktemp -d)" && git init -q
PYTHONPATH=/tmp/ts/src python3 -m aec init --hooks --hook-style <line|inline> .
# fingerprint .aec/snippets/claude-settings.json with the step 1 script
```

The rendered fingerprint must equal step 1's. Run Telosmith only from Zach's own repository at the pinned commit; installing anything from elsewhere needs his approval first (the agents' lane rules, §17).

If they differ, **stop**: don't add the fingerprint. An explanation doesn't satisfy this step, even a true one (a local edit, a different `--hook-style`, a render from another commit): the allowlist may hold only what the caller's pinned source produces. Name the differing bytes and report them to the repository's owner lane, who fixes the cause: re-render the hook at the pin, or move the pin in its own PR. Then repeat steps 1 and 2 until the two fingerprints match.

A hook written by hand for one repository has no render to compare with. It can only be scoped to that repository (step 3), and its exact text goes in the record's fixture (step 4) so that the reviewer reads what will run.

### 3. Scope

- **`"*"`** for a hook any Kairos repository may adopt unchanged: a Telosmith-rendered hook, traced in step 2. Its text names no repository, so allowing it everywhere allows nothing new to one repository that another doesn't already have.
- **The repository's own entry** (`"eraevous/<name>"`) for a hook written for one repository, or one that only makes sense there (it runs that repository's own scripts).

Say which, and why, in the PR body. Add the fingerprint to both production suites (`suites/kairos-v1.json` and `suites/kairos-records-v1.json`) unless there's a reason one shouldn't carry it; say the reason.

### 4. Record

JSON has no comments, so the record lives in the table at the end of this file, one row per fingerprint, in the same PR as the entry: the fingerprint, its scope, the source (repository and commit it was rendered from, or the PR that wrote it by hand), the hook's exact text as a fixture file under `tests/fixtures/hooks/`, the PR it serves, the date, and "approved by merge (Zach)".

`tests/test_hook_guard.py` enforces what it can:
- every fingerprint in either production suite has exactly one row, and every row has an entry;
- every row added under this protocol names a source commit, and a fixture whose bytes hash to the row's fingerprint.

It can't re-render the hook (CI has no Telosmith at every caller's pin), so the step 2 comparison stays the author's evidence in the PR body, and the reviewer checks it. Rows that predate this file have no fixture and are listed in the test; that list may only shrink.

### 5. Test

In `tests/test_hook_guard.py`, add the hook's exact command text as a fixture under `tests/fixtures/hooks/` and two cases, written first:

- **must pass:** the hook, run through `hook_guard` with each production suite;
- **must fail:** the same hook with one byte changed (a decoy).

Write them before the suite entry: run them once and see the must-pass case fail, then add the entry and see both pass. Say in the PR that you did.

### 6. Review

Open one PR, marked ready (not draft), titled `hook-guard: allow <what> (<first 8 of the fingerprint>…)`. Its body names the fingerprint in full, the steps above with their results, the callers it affects (an addition only widens the allowlist, so no green repository turns red), and the checks you ran. Request Zach's review (`requested_reviewers`) and ask Codex to review it (`@codex review`). Zach's merge is the approval. Agents never merge.

### 7. Afterwards

Once the repository that needed the hook has merged it, look for fingerprints no repository uses any more:

- read `.claude/settings.json` (and any committed `settings.local.json`) on the default branch of **every** repository that calls Lydian, **and** on their open PRs, before removing anything;
- remove only fingerprints that none of those use, in their own PR, naming each one and what replaced it (#10 is the precedent, after the Telosmith agent's (`claude:aec`) 63AR check);
- remove its row in the table in the same PR.

A removal can turn a repository red, so the PR names every caller checked and how.

## When a pin moves

A repository's Telosmith pin (`tools/aec_upstream.json`) can move. The rendered hook may or may not change with it:

- **The line launcher** (`--hook-style line`) calls `aec hook project` from the installed Telosmith, so the body can change with the package while the line itself stays the same. Checked 2026-11-22: Telosmith `b91f78b` and `e162493` (main) render the same line, `6a932ff5…`.
- **The inline hook** (`--hook-style inline`) carries the whole body, so most changes to it change the fingerprint.

For a pin move, re-render the hook at the new commit (step 2). If the fingerprint is unchanged, nothing here changes; say so in the pin PR. If it changed, follow steps 1–6 for the new fingerprint **before** the pin PR merges, so the repository's checks don't go red, and then step 7 for the old one.

A fingerprint taken from a PR head that later moved is obsolete: allow the fingerprint at the head that will merge. (`3fd17010d952…` was MtG#59's hook at `34534c1`; #59 re-rendered it at `78cc1ab` as `6a932ff5…`, which #20 allowed, so `3fd17010` was never added.)

## Allowlist records

Rows for entries added before this file existed were reconstructed on 2026-11-22 from the commits and PRs that added them. "In use" is from each caller's default branch that day.

| Fingerprint | Scope | Source | Fixture | Added in | Date merged | In use (2026-11-22) | Approval |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `c044525e192a71af29609b04621141db6b9464fe896effc892895c6f3b9d51ca` | eraevous/MtG | seeded from MtG's own hook as it stood | none (predates this file) | #7 | 2026-11-12 | MtG | approved by merge (Zach) |
| `c1b556106e40066c153423a0761a11313c0af0ab1a8d0ff1b0cce7c57d42a7fb` | eraevous/Telosmith | seeded from Telosmith's own repository hook | none (predates this file) | #7 | 2026-11-12 | Telosmith | approved by merge (Zach) |
| `5bd1822b388c63f2cedbda2118d17cbaf4574feddacf8dd04376263a5e639e29` | `"*"` and eraevous/crank-mem-eval | Telosmith v3 hook snippet, seeded for every repository | none (predates this file) | #7 | 2026-11-12 | crank-mem-eval | approved by merge (Zach) |
| `3b62d0a13d1845ec31489f25be40e90bce3bb872251fdea0ea4a0fb8d0237fbc` | eraevous/kairos-infra | seeded from the hook several repositories used then; the others' entries were dropped in #10 | none (predates this file) | #7 | 2026-11-12 | none on main | approved by merge (Zach) |
| `cb8eaebeb5becbf1c2363d3a465d8f2189da380877a716a77afb47372545d3fb` | `"*"` | Telosmith hook (Telosmith#77 at `6b737b6`) | none (predates this file) | #9 | 2026-11-14 | none on main | approved by merge (Zach) |
| `e019ae40b515a54546c2e53699bca6863e6568da534264afd4bbc2d744719896` | `"*"` | Telosmith main inline hook | none (predates this file) | #10 | 2026-11-15 | 11 repositories | approved by merge (Zach) |
| `599883c23c35ff4f4a2e45b4d6ec499c24bddb2e4e2a98d16bb383e71b4622d5` | `"*"` | Telosmith main inline hook after Telosmith#87 | none (predates this file) | #13 | 2026-11-17 | none on main | approved by merge (Zach) |
| `6a932ff50b8cb633df087ff190346e8700ab0eaebf96f42b0d626fb2e68ea454` | `"*"` | Telosmith line launcher, `aec init --hook-style line` at `b91f78b` (traced byte-identical 2026-11-22) | `telosmith-line-b91f78b.txt` | #20, for MtG#59 | 2026-11-22 | MtG | approved by merge (Zach) |
