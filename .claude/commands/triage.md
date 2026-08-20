---
description: Weekly autonomous triage of claude-usage — merge no-brainers to DEV, run tests, Codex collab, close duplicates / scope-violations, bump CHANGELOG by patch, push DEV. Leaves DEV→main release decision for the maintainer.
---

# /triage — weekly claude-usage triage

Designed to be run **headless** via Windows Task Scheduler (`claude -p "/triage"`) once a week. Operates in the local working copy on the `DEV` branch only. **Never pushes to `main`.**

## Identity & tone

- Sign every public-facing comment with `_— Claude Code & Codex collab_` on its own italicized line.
- Mention the version that ships next (the bumped patch version, e.g. `v1.1.1`) in close messages so contributors know where to look.
- Friendly, brief, honest. No emojis.
- Never dismiss a contributor's work as "wrong" when it duplicates a landed fix — they got there independently; thank them.

## Hard safety rails (do not violate)

1. **Dirty-worktree guard.** Before any `checkout` / `reset` / `merge`, run `git status --porcelain`. If output is non-empty, **abort immediately** — there is local maintainer work the routine would otherwise destroy. Do not stash, do not `--force`. Exit with a self-comment (step 8) explaining why the run was skipped.
2. **Never push to `main`.** Final release is the maintainer's call. DEV gets pushed; main never does.
3. **Never close a PR or issue opened by the repo owner** (`gh repo view --json owner --jq .owner.login`). Those are intentional, not triageable.
4. **Never auto-merge a PR** that:
   - touches more than 8 files, OR
   - has more than 200 added lines, OR
   - mentions any of these keywords in title / body / diff (security or contract-sensitive — escalate): `auth`, `password`, `credential`, `cookie`, `token`, `secret`, `api key`, `oauth`, `bearer`, `login`, `permission`, `encrypt`, `session`, `csrf`, OR
   - introduces new top-level dependencies (`requirements.txt`, `pyproject.toml`, `package.json`), OR
   - modifies anything under `.github/workflows/`, `scripts/`, `.claude/`, OR
   - includes deletions or renames of existing files (not just additions/edits), OR
   - includes a database schema change (`init_db` body, new `CREATE TABLE`, new `ALTER TABLE`).
5. **Never push DEV if `python -m unittest discover -s tests -v` fails on `main` first.** Baseline must be green before any work.
6. **Never push DEV if the final test sweep after merges fails.** Roll back, leave nothing on DEV.
7. **Stop if any external dependency is missing** (`gh`, `codex`, `python`) — exit cleanly with a noted error rather than partial state.
8. **Codex sign-off is mechanical, not advisory.** Before any `gh pr close` / `gh issue close` fires, a file at `/tmp/triage-codex-signoff.md` must exist containing (a) every item on the close list, and (b) the exact phrase `Codex sign-off: close list approved` on its own line. Codex generates this in step 2. No file → no closes. If Codex says "uncertain" on any item, that item stays open regardless.
9. **If unclear, leave it open and comment.** Don't guess. Surfacing as "needs maintainer review" is always preferable to a wrong close.

## Workflow

### 0. Pre-flight

Examples are bash; use the PowerShell equivalents on Windows (`Get-Command codex`, `if ((git status --porcelain).Length -gt 0)`, `$env:TEMP` instead of `/tmp`, etc.). Headless Claude must translate to the host shell — never literal-run the wrong syntax.

```
gh auth status                                   || exit 1
command -v codex                                 || exit 1
[ -z "$(git status --porcelain)" ]               || exit 1   # SAFETY RAIL 1
git fetch origin
git checkout main && git pull --ff-only
python -m unittest discover -s tests             || exit 1   # SAFETY RAIL 5
git checkout DEV
git merge --ff-only origin/DEV                                # fast-forward to remote DEV
git merge --ff-only main                                      # bring DEV up to date with main
```

If either `merge --ff-only` fails (DEV diverged from origin/DEV or from main), **stop**: divergence means there's unreleased work the maintainer staged. Don't touch it. Post a self-comment (step 8) noting the routine paused.

**Never run `git reset --hard` anywhere in this workflow.** It can silently destroy local work even after the dirty-worktree check. The temp-branch pattern in step 3 makes reset unnecessary.

### 1. Survey

```bash
gh issue list --state open --limit 100 --json number,title,author,body,createdAt > /tmp/triage-issues.json
gh pr list   --state open --limit 100 --json number,title,author,body,additions,deletions,changedFiles,headRefName,createdAt > /tmp/triage-prs.json
```

### 2. Triage with Codex (mandatory)

Write the survey to a brief file and consult Codex. The brief must:
- List every open PR and issue with its title, author, additions/deletions, and 1-line summary.
- Propose a per-item classification: **bug-fix in scope** / **duplicate of landed fix** / **feature (out of scope per project policy)** / **support question (leave open)** / **deferred refactor (close as out of scope for routine triage)** / **needs maintainer judgment (escalate via self-comment, do not close)**.
- For each "bug-fix in scope": say whether it should be merged whole (`gh pr checkout` + `git merge --no-ff`), cherry-picked (specific commit SHA), or applied manually with co-author trailer.
- For each "duplicate": which landed PR supersedes it.

Use `~/.claude/skills/codex-ideation/scripts/codex.py --new --read <brief-file> --sandbox workspace-write`. Then iterate (`codex.py "<reaction>"`) until you and Codex agree on every line. Convergence = both of you would defend the punch list.

**Generate the sign-off artifact required by safety rail 8.** Once converged, write `/tmp/triage-codex-signoff.md` with the full close list (one item per line: `<#> — <action> — <reason>`) and the literal line `Codex sign-off: close list approved` at the end. Step 4 reads this file before each close and aborts if anything is missing or if any item is marked "uncertain".

**If Codex disagrees on more than ~30% of items, abort the run and post a single self-comment** (see step 8) saying "weekly triage paused — Codex and Claude disagreed on triage; needs maintainer review." Do not write the sign-off file.

### 3. Execute merges on a disposable branch

**Never merge directly to `DEV`** — too easy to leave it in a half-merged state if a later step fails. Use a temporary branch instead:

```
git checkout -b triage-run/$(date +%Y-%m-%d) DEV    # bash
# OR (PowerShell):
git checkout -b "triage-run/$(Get-Date -Format yyyy-MM-dd)" DEV
```

All merges, tests, and follow-up commits land on this temp branch. If any step fails, the temp branch gets deleted and `DEV` is untouched — no rollback gymnastics needed.

For each agreed bug-fix:

```
git fetch origin pull/<N>/head:pr-<N>
git merge --no-ff pr-<N> -m "Merge pull request #<N> from <author>/<branch>

<title>

<one-paragraph why this fix is correct>"
python -m unittest discover -s tests
```

If `unittest` fails, **abort the entire run** (don't try to recover this PR and keep going):
- Delete the temp branch (`git checkout DEV && git branch -D triage-run/<date>`).
- `DEV` is exactly where it was before the run started.
- Post a self-comment (step 8) noting which PR's merge broke tests.

For partial merges, `git cherry-pick <commit-sha>` against the specific upstream commit (preserves authorship). If the fix isn't a clean commit, apply hunks manually with `git commit --author="<name> <email>"` to preserve attribution; never silently take credit.

**Add a regression test if one isn't already present.** Verify by temporarily reverting the fix (`git revert --no-commit <sha>` then run tests then `git revert --abort`) — the test must fail without the fix. Re-apply if needed, commit the test as a separate Claude+Codex co-authored commit, continue.

### 3a. (Reserved) DEV fast-forward happens in step 7, not here.

The temp branch keeps all merges + tests + the CHANGELOG bump + any Codex-review fixups. `DEV` only moves once at the very end (step 7) when the full sequence is green.

### 4. Stage close messages (do not execute yet)

Build the list of items to close (duplicate PRs, superseded fixes, scope-frozen features, fixed issues) with the exact message text for each. Save to `/tmp/triage-closures.json` as an array of `{type: "pr"|"issue", number, body}`.

**Do not call `gh pr close` / `gh issue close` here.** Those execute in step 7a, only after the push has succeeded. A failed push must not leave closed items pointing at a release that never shipped.

Templates (use the actual landed PR number and bumped version):

**Duplicate bug-fix PR:**
> Thanks for catching this and proposing a fix — really appreciated. The same fix landed via #<N> from @<author> for **v<X.Y.Z>**. Your fix would have worked too; this isn't a quality call. Closing as superseded.
>
> _— Claude Code & Codex collab_

**Feature PR (scope-freeze):**
> Thanks for putting this together. We're holding the product surface tightly to its core ("parse local JSONLs, show usage and cost"). Feature additions should start as a [GitHub Discussion](https://github.com/phuryn/claude-usage/discussions) so the concept can be validated with other users before code lands. Closing here; a re-submitted PR is welcome after a Discussion gains traction.
>
> _— Claude Code & Codex collab_

**Definitely-fixed issue:**
> Closing as fixed in **v<X.Y.Z>** — see the linked PR. Please reopen if it still reproduces after pulling `DEV`.
>
> _— Claude Code & Codex collab_

**Likely-fixed issue (matches a fixed signature but the reporter didn't diagnose):**
> Closing optimistically — your symptom matches \<concrete-bug-name\> which was fixed for **v<X.Y.Z>**. Please reopen with browser-console output / `cli.py stats` output if it still reproduces.
>
> _— Claude Code & Codex collab_

**Out-of-scope feature-request issue:**
> Thanks for raising this. We're holding scope tight on this tool. Feature requests are best validated as a [GitHub Discussion](https://github.com/phuryn/claude-usage/discussions) first; closing this issue, please open a Discussion if you'd still like to pursue it.
>
> _— Claude Code & Codex collab_

### 5. CHANGELOG bump

- Read `CHANGELOG.md`, find the latest version header (regex `## v(\d+)\.(\d+)\.(\d+)`).
- Increment the **patch** segment only (`v1.1.0 → v1.1.1`). If nothing was actually merged in step 3, **skip the bump** and skip step 7 (push).
- Insert a new section at the top: `## v<X.Y.Z+1> — TBD` with bullets grouped by area (Dashboard / Scanner / Project), one bullet per landed change with PR ref and author thanks. The maintainer fills in the date when releasing; the auto-tag workflow then creates the matching git tag on the merge to main. See "Versioning and releases" in AGENTS.md for the full release flow.
- Commit as `docs(CHANGELOG): bump to v<X.Y.Z+1>` with Claude + Codex co-author trailers.

### 6. Codex review of cumulative diff

Diff the **temp branch HEAD** (which is what step 3a will fast-forward DEV to), not DEV itself — DEV is still pointing at the pre-run commit:

```
git diff origin/main..HEAD > /tmp/triage-diff.diff
# Ask Codex: "Anything that should NOT ship? CHANGELOG accurate? Tests cover the new behavior? Under 200 words."
```

**Apply any concrete fixes Codex flags as new commits on the temp branch** before proceeding. Re-run the full test suite after each fix.

### 7. Fast-forward DEV and push

Only if (a) merges happened on the temp branch, (b) full test suite passes, (c) Codex review found nothing blocking:

```
git checkout DEV
git merge --ff-only triage-run/<date>
git branch -d triage-run/<date>
git push origin DEV
```

### 7a. Execute the staged closes

Now — and only now — read `/tmp/triage-closures.json` and fire each `gh pr close <N> --comment <body>` / `gh issue close <N> --comment <body>` in order. If any close fails (rate-limit, permissions, etc.), record it for the step 8 summary; don't roll back the push.

### 8. Notify the maintainer

After a successful push **OR after any abort that happened with actionable items in queue** (i.e. not the "nothing to do" case in step 9), leave a single self-comment on the most-recent maintainer-authored open issue (or a tracking issue if one exists tagged `routine`) summarizing:
- Version bumped to: `v<X.Y.Z+1>`
- PRs merged (with #s and authors)
- PRs/issues closed (counts by category)
- Anything escalated for maintainer review
- One-line: "DEV is ready for release decision."

Sign with the standard signature.

### 9. If nothing happened

If step 3 merged zero PRs and step 4 closed zero items, do not push, do not bump CHANGELOG, do not notify. Exit silently. Routine ran, found nothing actionable, no noise.

## What stays the maintainer's call

- DEV → main merges (releases). The routine never does this.
- Anything Codex disagreed on, or anything with a security/auth keyword in it.
- Issues #92 (Dashboard Not Reporting New Data — possibly upstream) and similar diagnostic-required reports.
- Refactors and architecture changes.
- New features and feature requests (those go to Discussions).
