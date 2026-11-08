# git-janitor

[![tests](https://github.com/jayqi/git-janitor/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/jayqi/git-janitor/actions/workflows/tests.yml?query=branch%3Amain)
[![coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/jayqi/git-janitor/badges/coverage.json)](https://github.com/jayqi/git-janitor/actions/workflows/tests.yml?query=branch%3Amain)

**Gather evidence on whether local git branches and worktrees are merged on GitHub, and clean up the ones that are.**

git-janitor gives every local branch and worktree a verdict, and deletes only the ones it can verify are merged and safe to remove. It's designed to be used by coding agents first, with JSON output, and by people second.

```console
$ git janitor report
┏━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ Candidate   ┃ Verdict ┃ Status         ┃ Worktree          ┃ Blockers ┃ Pull request ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ add-retries │ delete  │ merged         │                   │          │ #1           │
│ fix-timeout │ review  │ merged         │ ../wt-fix-timeout │ dirty    │ #2           │
│ main        │ keep    │ default_branch │ .                 │          │              │
│ new-parser  │ keep    │ pr_open        │                   │          │ #3           │
│ spike       │ review  │ no_pr          │                   │          │              │
└─────────────┴─────────┴────────────────┴───────────────────┴──────────┴──────────────┘

$ git janitor clean --all
add-retries: deleted
  to recover: git branch add-retries efbdaba1a8908b6ddc54a44f1e9b87f3d1d1eb7b
```

## Why git-janitor

Common ways to clean up merged branches each miss something:

- **`git branch -d` and `git branch --merged`** can't tell a branch was merged with a squash or rebase merge, because its commits land as new commits.
- **Deleting branches whose upstream is gone** (`git fetch --prune`, then deleting `[gone]` branches) also deletes branches whose pull request was closed without merging, and loses commits that were never pushed.
- **Matching merged pull requests by branch name** treats a new branch that reuses an old name as merged.
- **`git worktree remove --force`** deletes untracked and ignored files, like notes or plans you kept in the worktree.

git-janitor checks that a merged pull request contains each branch's exact tip, and that each worktree holds nothing that would be lost, before deleting anything. When it does delete:

- It never forces a deletion, and has no option to.
- It never modifies anything on GitHub or origin.
- It prints a command to recreate each deleted branch.

## Installation

You'll need:

- `git` 2.36 or newer
- GitHub's [`gh` CLI](https://cli.github.com/), installed and logged in
- A repository whose `origin` remote is on GitHub

Install with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install git+https://github.com/jayqi/git-janitor
```

Because the executable is named `git-janitor`, git runs it as a subcommand: `git janitor ...`.

## Usage

```sh
git janitor report                  # report on every local branch and worktree
git janitor report feat-x ../wt     # report on specific branches or worktree paths
git janitor report --json           # machine-readable output

git janitor clean feat-x            # delete specific candidates, if their verdict is `delete`
git janitor clean --all             # delete every candidate whose verdict is `delete`
git janitor clean --all --dry-run   # show what would be deleted
git janitor clean feat-x --accept precious_files   # delete despite reviewed precious files
```

To run against a repository other than the current directory, use `git -C <path> janitor ...`.

For help, run `git janitor -h` or `git janitor <command> -h`. `git janitor --help` doesn't work, because git turns it into `git help janitor`, which looks for a man page that isn't installed.

> [!NOTE]
> Both commands run `git fetch origin --prune` first. Pass `--no-fetch` to skip it. Even with `--no-fetch`, git-janitor may fetch a pull request's head commit (`refs/pull/<N>/head`) when it's missing locally. If that fetch fails, the pull request is treated as not matching.

## How git-janitor decides

The report has one row, a **candidate**, per thing that could be cleaned up:

- a branch with no worktree,
- a branch and the worktree it's checked out in, or
- a worktree with a detached HEAD, or whose branch doesn't exist (`branch_missing`).

Each candidate gets a verdict:

| Verdict | Meaning |
|---|---|
| `delete` | Status is `merged` or `no_unique_commits`, and there are no blockers. `clean` will delete it. |
| `keep` | The work is active (`pr_open`), or it's the default branch. |
| `review` | Everything else. A person, or an agent investigating the evidence, needs to decide. |

The verdict comes from the candidate's **status** and its **blockers**.

### Statuses

| Status | Meaning | Verdict |
|---|---|---|
| `merged` | A matching pull request was merged, and the merge reached the default branch (see below). | `delete`, unless blocked |
| `no_unique_commits` | No matching pull request, and the tip is already in the default branch, so deleting it loses no commits. This includes branches created but not yet committed to, and branches fast-forwarded to the default branch. | `delete`, unless blocked |
| `merged_plus_local` | A pull request was merged, and the merge reached the default branch, but the local tip has commits beyond its head. | `review` |
| `pr_open` | A matching pull request is open, or an open pull request's head is behind the local tip. Any open matching pull request takes precedence over closed or merged ones. | `keep` |
| `pr_closed` | A matching pull request was closed without merging. | `review` |
| `merged_into_other_base` | A matching pull request was merged into a branch other than the default branch, and its merge commit isn't in the default branch. | `review` |
| `no_pr` | No matching pull request, and the tip has commits that aren't in the default branch. | `review` |
| `default_branch` | This is the default branch. It is never deleted. | `keep` |
| `branch_missing` | A worktree whose branch has no commits yet or no longer exists as a local branch. | `review` |

### What counts as merged

For each branch, git-janitor looks up pull requests on origin whose head branch has the same name as the local branch or its upstream on origin. A branch is `merged` when both of these are true:

1. **A pull request contains the branch's local tip.** The tip is the pull request's head commit, or an ancestor of the head that isn't already in the default branch.
2. **That pull request's merge reached the default branch.** Its base is the default branch, or its merge commit is in the default branch.

With squash and rebase merges, the branch's own commits never appear in the default branch. What's checked is that the pull request containing them was merged.

> [!IMPORTANT]
> A merged pull request with the same branch name isn't enough. Requiring that the tip isn't already in the default branch stops a branch created from older history, such as a new branch reusing a merged pull request's name, from matching. Pull requests that share the name but don't match are reported as evidence and otherwise ignored, so a reused branch name can't make new work look merged.

Only pull requests into origin itself count, not ones into other repositories in its fork network. The default branch comes from GitHub.

#### Stacked pull requests

When GitHub merges a stack of pull requests, each one keeps the branch below it as its base, but its merge commit is created on the default branch. Rule 2 counts these as merged.

It also counts a pull request merged into a branch whose history, including that merge commit, was later merged into the default branch. If that branch was squash- or rebase-merged instead, the merge commit isn't in the default branch, and the pull request is `merged_into_other_base`.

#### Detached worktrees

A worktree with a detached HEAD is judged by its HEAD commit, against the pull requests GitHub associates with that commit: a pull request's own commits and its merge commit. An example is a worktree checked out at a pull request's head for review.

A worktree in the middle of a rebase or bisect is detached too, but git-janitor reports it with the branch the rebase or bisect started from.

### Blockers

Blockers are reasons a worktree shouldn't be removed yet. Any blocker downgrades a `merged` or `no_unique_commits` candidate to `review`.

| Blocker | Meaning |
|---|---|
| `dirty` | The worktree has modified or staged tracked files. |
| `untracked` | The worktree has untracked files that aren't ignored. A directory with nothing tracked in it is listed once, ending in `/`. |
| `precious_files` | The worktree has ignored files that look like personal work (see [Precious ignored files](#precious-ignored-files)). |
| `locked` | The worktree is locked with `git worktree lock`. A locked worktree whose directory is missing gets only this blocker. |
| `in_progress_op` | A rebase, merge, cherry-pick, revert, or bisect is in progress in the worktree. |
| `is_current` | The directory git-janitor runs in is inside the worktree. With `git -C <path>`, that's `<path>`, not your shell's current directory. |
| `is_main` | The branch is checked out in the main worktree, which is never removed. |
| `nested_repository` | A git repository is inside a precious ignored directory. |
| `unreadable` | Some ignored paths couldn't be read, so git-janitor can't tell whether they're precious. |
| `recent_activity` | A `no_unique_commits` worktree, which an agent may have just created, was active within `janitor.minAge` (see [Activity](#activity)), or has no recorded activity. |
| `error` | Checking the worktree failed. `detail` has the error. |

#### Activity

Each candidate's `last_activity` is the latest of:

- its branch's reflog,
- its linked worktree's HEAD reflog, and
- when its linked worktree was created.

These are read from git's records for the worktree, so they work when the worktree's directory is missing, and ordinary commands like `git status` don't change them. Activity in the main worktree isn't included.

`no_unique_commits` worktrees only get the `delete` verdict once they've been inactive for `janitor.minAge`, 7 days by default. See [Configuration](#configuration).

#### Precious ignored files

`git worktree remove` deletes ignored files without warning, so git-janitor checks them first. An ignored file is classified as follows:

- Matched by a `.gitignore` file committed to the repository: **disposable**. These are things the project generates, like `.venv/` or `build/`, and git-janitor doesn't look inside them. A `.gitignore` that isn't committed, such as `notes/.gitignore` containing `*`, doesn't count.
- Matched by your global excludes file (`core.excludesFile`) or `.git/info/exclude`: **precious**. These are things you set aside personally, like `.plans/`.
- Inside a cache directory, marked by a [`CACHEDIR.TAG`](https://bford.info/cachedir/) file: **disposable**. Tools like uv, ruff, and pytest create these, along with an uncommitted `.gitignore` that ignores their contents.
- Common operating-system, editor, and virtual environment files: always **disposable**, even when matched by your global excludes. These are `.DS_Store`, `Thumbs.db`, `desktop.ini`, `.idea/`, `.vscode/`, `.venv/`, and `*~`. Editor swap files like `*.swp` are not, since they may hold unsaved changes.
- Anything git-janitor can't attribute to an ignore rule: **precious**.

Precious files are reported per ignored path, as `git status --ignored` lists it. A directory like `.plans/` is one entry with its number of precious files, their total size, and the latest modification time. Git repositories and other worktrees inside precious directories are reported as `nested_repository` instead.

To mark more patterns as disposable, use `janitor.disposable`. See [Configuration](#configuration).

## Cleaning up

`git janitor clean` takes candidate ids, or `--all` for every candidate whose verdict is `delete`. Running it with neither, or with both, is an error.

- It always re-runs the analysis and refuses any target whose verdict isn't `delete`. There is no option to force it.
- Right before removing a worktree, it checks again that the worktree has no blockers other than accepted ones. It deletes a branch only if the branch still points at the analyzed tip and isn't checked out anywhere. Otherwise the target is `failed`.
- It removes the worktree with `git worktree remove` (without `--force`), then deletes the branch and its `branch.<name>` config.
- It only touches local branches and worktrees. It never deletes anything on GitHub.
- It prints the tip commit of each deleted branch and a command to recreate it, such as `git branch feat-x 4f1c0de`. This works until git garbage-collects the commit.

### Accepting blockers

`--accept <codes>` ignores the listed blockers for explicitly named targets, after you or an agent have reviewed them.

- Only two blockers can be accepted: `precious_files`, whose contents git-janitor lists, and `recent_activity`, which only protects worktrees that may be in use.
- For other blockers, such as `dirty`, `untracked`, or `nested_repository`, move, commit, or delete what they report first.
- `--accept` can't be combined with `--all`.
- Outcomes list the accepted blockers in full in `accepted`, with the same fields as in the report, and keep the candidate's own `verdict`.

> [!WARNING]
> Accepting `precious_files` covers whatever precious files exist when `clean` runs, including ones added since the report you reviewed.

## Configuration

git-janitor reads these git config keys:

| Key | Meaning |
|---|---|
| `janitor.minAge` | How long a `no_unique_commits` worktree must be inactive before it can be deleted. A duration like `3d`, `12h`, `30m`, or `0`. Default: `7d`. |
| `janitor.disposable` | Extra file or directory name patterns for ignored files that are always disposable. Can be set multiple times. Each pattern is matched against single file and directory names, so a pattern containing `/` never matches. |

```sh
git config --global janitor.minAge 3d
git config --global --add janitor.disposable '.envrc'
```

## JSON output

### `report --json`

`git janitor report --json` prints an object like this:

```json
{
  "schema_version": 1,
  "repo": "/Users/me/repos/project",
  "github_repo": "owner/project",
  "default_branch": "main",
  "fetched": true,
  "candidates": [
    {
      "id": "feat-x",
      "verdict": "delete",
      "status": "merged",
      "branch": {
        "name": "feat-x",
        "tip": "4f1c0de...",
        "upstream": "origin/feat-x",
        "upstream_gone": true
      },
      "worktree": {
        "path": "/Users/me/repos/project-feat-x",
        "head": "4f1c0de...",
        "detached": false,
        "is_main": false,
        "locked": false,
        "prunable": false
      },
      "blockers": [],
      "evidence": [
        {
          "kind": "pull_request",
          "number": 42,
          "url": "https://github.com/owner/project/pull/42",
          "state": "MERGED",
          "base": "main",
          "head_oid": "4f1c0de...",
          "merge_oid": "9a2b7e1...",
          "matched": true
        }
      ],
      "actions": [
        {"kind": "remove_worktree", "path": "/Users/me/repos/project-feat-x"},
        {"kind": "delete_branch", "name": "feat-x", "tip": "4f1c0de..."}
      ],
      "last_activity": "2026-10-20T14:03:12+00:00"
    }
  ]
}
```

- `repo` is the path of the main worktree, wherever the command was run from.
- `id` is the branch name, or the worktree path for a detached worktree. Pass it to `report` or `clean` as a target.
- `branch` or `worktree` is `null` when the candidate doesn't have one.
- `blockers` are objects with a `code` from the [blockers table](#blockers) and a human-readable `detail`. Some have more fields:
  - `paths`, relative to the worktree: `dirty`, `untracked`, `nested_repository`, and `unreadable`.
  - `entries`: `precious_files`. Each entry has a `path`, ending in `/` for a directory, its `files` count, total `size` in bytes, and latest `modified` time.
  - `operation`: `in_progress_op`. One of `merge`, `rebase`, `cherry-pick`, `revert`, or `bisect`.
  - `min_age`: `recent_activity`, like `7d`.
- `evidence` holds the pull requests found for the branch name, or for a detached worktree, associated with its HEAD commit. They're newest first, whether or not they matched.
  - `merge_oid` is the pull request's merge commit, or `null` if it isn't merged.
  - When the local tip is beyond a pull request's head (`merged_plus_local`, or `pr_open` with unpushed commits), `evidence` also holds an `extra_commits` entry. It has that `pull_request` number and a `commits` list with the `sha` and `subject` of each commit beyond the head that isn't in the default branch, oldest first.
- `actions` lists, in order, what `clean` would do.
- `last_activity` is an ISO 8601 time, or `null` if git has no record of activity.

### `clean --json`

`git janitor clean --json` prints the outcome for each target:

```json
{
  "schema_version": 1,
  "dry_run": false,
  "outcomes": [
    {
      "id": "feat-x",
      "outcome": "deleted",
      "verdict": "delete",
      "detail": "",
      "actions": [{"kind": "delete_branch", "name": "feat-x", "tip": "4f1c0de..."}],
      "recovery": ["git branch feat-x 4f1c0de..."],
      "accepted": []
    }
  ]
}
```

`outcome` is one of:

- `deleted`
- `would_delete`, with `--dry-run`
- `refused`: the verdict isn't `delete`
- `not_found`, with `verdict: null`
- `failed`: an action failed or its target changed since the analysis. `detail` says why.

`actions` are the candidate's planned actions. `recovery` covers only the actions that were carried out, or would be with `--dry-run`.

`schema_version` increases whenever the shape of either output changes incompatibly.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The command ran. For `report`, this is true whatever the verdicts are. |
| `1` | The tool couldn't run (not a git repository, `gh` missing or not logged in, fetch failed, origin's default branch not fetched), a target doesn't exist, or `clean` refused or failed to delete a target. |
