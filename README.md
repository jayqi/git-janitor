# git-janitor

**Gather evidence on whether local git branches and worktrees are merged on GitHub, and clean up the ones that are.**

GitHub's squash and rebase merges create new commits on the default branch, so git's own ancestry checks (`git branch --merged`, `git branch -d`) can't tell that a branch's work has landed. git-janitor asks GitHub which pull requests contain each branch's commits, checks each worktree for work that would be lost, and gives every branch and worktree a verdict. A guarded `clean` command deletes only what it can verify is safe.

It's designed to be used by coding agents first, with JSON output, and by people second.

## Prerequisites

- `git`
- GitHub's [`gh` CLI](https://cli.github.com/), installed and logged in
- A repository whose `origin` remote is on GitHub

## Installation

With [uv](https://docs.astral.sh/uv/):

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
```

Both commands run `git fetch origin --prune` first. Pass `--no-fetch` to skip it.

To run against a repository other than the current directory, use `git -C <path> janitor ...`.

## How merge status is decided

A branch counts as merged only if **its commits are in origin's default branch**. A merged pull request with the same branch name is not enough: the pull request must contain the branch's local tip.

For each branch, git-janitor looks up pull requests on origin whose head branch has the same name as the local branch or its upstream on origin. A pull request **matches** if the local tip is the pull request's head commit or an ancestor of it. Pull requests that share the name but don't match are reported as evidence and otherwise ignored, so a reused branch name can't make new work look merged.

The default branch comes from GitHub.

### Statuses

| Status | Meaning |
|---|---|
| `merged` | A matching pull request was merged into the default branch. |
| `merged_plus_local` | A pull request merged into the default branch, but the local tip has commits beyond its head. |
| `pr_open` | A matching pull request is open. |
| `pr_closed` | A matching pull request was closed without merging. |
| `merged_into_other_base` | A matching pull request was merged into a branch other than the default branch. |
| `no_unique_commits` | No matching pull request, and the tip is already in the default branch. This includes branches created but not yet committed to. |
| `no_pr` | No matching pull request, and the tip has commits that aren't in the default branch. |
| `default_branch` | This is the default branch. It is never deleted. |

A worktree with a detached HEAD is judged by its HEAD commit. It has no branch name, so it can only be `no_unique_commits` or `no_pr`.

## Candidates and verdicts

The report has one row, a **candidate**, per thing that could be cleaned up:

- a branch with no worktree,
- a branch and the worktree it's checked out in, or
- a worktree with a detached HEAD.

Each candidate has a verdict:

| Verdict | Meaning |
|---|---|
| `delete` | Status is `merged` and there are no blockers. `clean` will delete it. |
| `keep` | The work is active (`pr_open`), or it's the default branch with nothing to clean up. |
| `review` | Everything else. A person, or an agent investigating the evidence, needs to decide. |

### Blockers

Any blocker downgrades a `merged` candidate to `review`.

| Blocker | Meaning |
|---|---|
| `dirty` | The worktree has modified or staged tracked files. |
| `untracked` | The worktree has untracked files that aren't ignored. |
| `precious_files` | The worktree has ignored files that look like personal work (see below). |
| `locked` | The worktree is locked with `git worktree lock`. |
| `in_progress_op` | A rebase, merge, cherry-pick, revert, or bisect is in progress in the worktree. |
| `is_current` | The current directory is inside the worktree. |
| `is_main` | The branch is checked out in the main worktree, which is never removed. |
| `stashes` | There are stash entries made on this branch. |

### Precious ignored files

`git worktree remove` deletes ignored files without warning, so git-janitor checks them first. An ignored file is classified by which ignore rule matched it:

- Matched by a `.gitignore` file in the repository: **disposable**. These are things the project generates, like `.venv/` or `build/`.
- Matched by your global excludes file (`core.excludesFile`) or `.git/info/exclude`: **precious**. These are things you set aside personally, like `.plans/`.
- Common operating-system and editor files are always disposable, even when matched by your global excludes: `.DS_Store`, `Thumbs.db`, `desktop.ini`, `.idea/`, `.vscode/`, `*.swp`, `*.swo`, `*~`.

To mark more patterns as disposable, add them to git config:

```sh
git config --global --add janitor.disposable '.envrc'
```

## JSON output

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
          "matched": true
        }
      ],
      "actions": [
        {"kind": "remove_worktree", "path": "/Users/me/repos/project-feat-x"},
        {"kind": "delete_branch", "name": "feat-x", "tip": "4f1c0de..."}
      ]
    }
  ]
}
```

- `id` is the branch name, or the worktree path for a detached worktree. Pass it to `report` or `clean` as a target.
- `branch` or `worktree` is `null` when the candidate doesn't have one.
- `blockers` are objects with a `code` from the table above, a human-readable `detail`, and, where relevant, `paths` (`dirty`, `untracked`), `files` (`precious_files`, each with `path`, `size`, and `modified`), `operation` (`in_progress_op`: `merge`, `rebase`, `cherry-pick`, `revert`, or `bisect`), or `stashes` (`stashes`, each with `ref` and `subject`).
- `evidence` holds the pull requests found for the branch name, newest first, whether or not they matched. For `merged_plus_local`, it also holds an `extra_commits` entry with the merged `pull_request` number and the `sha` and `subject` of each commit beyond its head, oldest first.
- `actions` lists, in order, what `clean` would do.

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
      "recovery": ["git branch feat-x 4f1c0de..."]
    }
  ]
}
```

`outcome` is one of `deleted`, `would_delete` (with `--dry-run`), `refused` (verdict isn't `delete`), `not_found`, or `failed` (git refused an action; `detail` has its error). `actions` are the candidate's planned actions. `recovery` covers only the actions that were carried out, or would be with `--dry-run`.

`schema_version` increases whenever the shape of either output changes incompatibly.

## Cleaning up

`git janitor clean` takes candidate ids, or `--all` for every candidate whose verdict is `delete`. Running it with neither is an error.

- It always re-runs the analysis and refuses any target whose verdict isn't `delete`. There is no option to force it.
- It removes the worktree with `git worktree remove` (without `--force`), then deletes the branch with `git branch -D`.
- It only touches local branches and worktrees. It never deletes anything on GitHub.
- It prints the tip commit of each deleted branch and a command to recreate it, such as `git branch feat-x 4f1c0de`. This works until git garbage-collects the commit.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The command ran. For `report`, this is true whatever the verdicts are. |
| `1` | The tool couldn't run (not a git repository, `gh` missing or not logged in, fetch failed), a target doesn't exist, or `clean` refused or failed to delete a target. |
