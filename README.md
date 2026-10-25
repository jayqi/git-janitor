# git-janitor

**Gather evidence on whether local git branches and worktrees are merged on GitHub, and clean up the ones that are.**

GitHub's squash and rebase merges create new commits on the default branch, so git's own ancestry checks (`git branch --merged`, `git branch -d`) can't tell that a branch's work has landed. git-janitor asks GitHub which pull requests contain each branch's commits, checks each worktree for work that would be lost, and gives every branch and worktree a verdict. A guarded `clean` command deletes only what it can verify is safe.

It's designed to be used by coding agents first, with JSON output, and by people second.

## Prerequisites

- `git` 2.36 or newer
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

Both commands run `git fetch origin --prune` first. Pass `--no-fetch` to skip it. Even with `--no-fetch`, git-janitor may fetch a pull request's head commit (`refs/pull/<N>/head`) when it's missing locally. If that fetch fails, the pull request is treated as not matching.

To run against a repository other than the current directory, use `git -C <path> janitor ...`.

## How merge status is decided

A branch counts as merged only if **GitHub reports a pull request containing its local tip as merged into origin's default branch**. A merged pull request with the same branch name is not enough: the pull request must contain the branch's local tip. With squash and rebase merges, the branch's own commits never appear in the default branch; what's checked is that the pull request containing them was merged.

For each branch, git-janitor looks up pull requests on origin whose head branch has the same name as the local branch or its upstream on origin. A pull request **matches** if the local tip is the pull request's head commit, or an ancestor of the head that isn't already in the default branch. The second condition stops a branch created from older history, such as a fresh branch reusing a merged pull request's name, from matching. Pull requests that share the name but don't match are reported as evidence and otherwise ignored, so a reused branch name can't make new work look merged.

The default branch comes from GitHub.

### Statuses

| Status | Meaning |
|---|---|
| `merged` | A matching pull request was merged into the default branch. |
| `merged_plus_local` | A pull request merged into the default branch, but the local tip has commits beyond its head. |
| `pr_open` | A matching pull request is open, or an open pull request's head is behind the local tip. Any open matching pull request takes precedence over closed or merged ones. |
| `pr_closed` | A matching pull request was closed without merging. |
| `merged_into_other_base` | A matching pull request was merged into a branch other than the default branch. |
| `no_unique_commits` | No matching pull request, and the tip is already in the default branch. This includes branches created but not yet committed to, and branches fast-forwarded to the default branch. |
| `no_pr` | No matching pull request, and the tip has commits that aren't in the default branch. |
| `default_branch` | This is the default branch. It is never deleted. |
| `branch_missing` | A worktree whose branch has no commits yet or no longer exists as a local branch. |

A worktree with a detached HEAD is judged by its HEAD commit. It has no branch name, so it can only be `no_unique_commits` or `no_pr`. A worktree in the middle of a rebase or bisect is detached too, but git-janitor reports it with the branch the rebase or bisect started from.

## Candidates and verdicts

The report has one row, a **candidate**, per thing that could be cleaned up:

- a branch with no worktree,
- a branch and the worktree it's checked out in, or
- a worktree with a detached HEAD, or whose branch doesn't exist (`branch_missing`).

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
| `locked` | The worktree is locked with `git worktree lock`. A locked worktree whose directory is missing gets only this blocker. |
| `in_progress_op` | A rebase, merge, cherry-pick, revert, or bisect is in progress in the worktree. |
| `is_current` | The current directory is inside the worktree. |
| `is_main` | The branch is checked out in the main worktree, which is never removed. |
| `stashes` | There are stash entries made on this branch. |
| `nested_worktree` | Other worktrees are inside this worktree's directory, so removing it would delete them too. |
| `multiple_worktrees` | The branch is checked out in more than one worktree (with `git worktree add --force`). |
| `broken_worktree` | The worktree's directory exists, but git no longer recognizes it as a worktree, for example because its `.git` file was deleted. |
| `hidden_changes` | Tracked files marked `--skip-worktree` or `--assume-unchanged` differ from the index. `git status` doesn't show these changes. |
| `submodules` | The worktree has populated submodules, which `git worktree remove` refuses to remove. |
| `nested_repository` | A git repository is inside an ignored directory. |
| `unreadable` | Some ignored paths couldn't be read, so git-janitor can't tell whether they're precious. |

### Precious ignored files

`git worktree remove` deletes ignored files without warning, so git-janitor checks them first. An ignored file is classified as follows:

- Matched by a `.gitignore` file committed to the repository: **disposable**. These are things the project generates, like `.venv/` or `build/`. A `.gitignore` that isn't committed, such as `notes/.gitignore` containing `*`, doesn't count.
- Matched by your global excludes file (`core.excludesFile`) or `.git/info/exclude`: **precious**. These are things you set aside personally, like `.plans/`.
- Anything inside a cache directory, marked by a [`CACHEDIR.TAG`](https://bford.info/cachedir/) file, is disposable. Tools like uv, ruff, and pytest create these, along with an uncommitted `.gitignore` that ignores their contents.
- Common operating-system and editor files are always disposable, even when matched by your global excludes: `.DS_Store`, `Thumbs.db`, `desktop.ini`, `.idea/`, `.vscode/`, `*.swp`, `*.swo`, `*~`.
- Anything git-janitor can't attribute to an ignore rule is precious.

Git repositories anywhere in ignored directories are reported as `nested_repository`, and other worktrees as `nested_worktree`, rather than file by file.

To mark more patterns as disposable, add them to git config. Each pattern is matched against single file and directory names, so a pattern containing `/` never matches.

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
- `repo` is the path of the main worktree, wherever the command was run from.
- `blockers` are objects with a `code` from the table above, a human-readable `detail`, and, where relevant, `paths` (relative to the worktree for `dirty`, `untracked`, `hidden_changes`, `submodules`, `nested_worktree`, `nested_repository`, and `unreadable`; absolute for `multiple_worktrees`), `files` (`precious_files`, each with `path`, `size`, and `modified`), `operation` (`in_progress_op`: `merge`, `rebase`, `cherry-pick`, `revert`, or `bisect`), or `stashes` (`stashes`, each with `ref` and `subject`).
- `evidence` holds the pull requests found for the branch name, newest first, whether or not they matched. When the local tip is beyond a pull request's head (`merged_plus_local`, or `pr_open` with unpushed commits), it also holds an `extra_commits` entry with that `pull_request` number and a `commits` list with the `sha` and `subject` of each commit beyond the head that isn't in the default branch, oldest first.
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

`outcome` is one of `deleted`, `would_delete` (with `--dry-run`), `refused` (verdict isn't `delete`), `not_found` (with `verdict: null`), or `failed` (an action failed or its target changed since the analysis; `detail` says why). `actions` are the candidate's planned actions. `recovery` covers only the actions that were carried out, or would be with `--dry-run`.

`schema_version` increases whenever the shape of either output changes incompatibly.

## Cleaning up

`git janitor clean` takes candidate ids, or `--all` for every candidate whose verdict is `delete`. Running it with neither, or with both, is an error.

- It always re-runs the analysis and refuses any target whose verdict isn't `delete`. There is no option to force it.
- Right before removing a worktree, it checks again that the worktree's HEAD commit and branch are unchanged and that it has no blockers. It deletes a branch only if the branch still points at the analyzed tip and isn't checked out anywhere. Otherwise the target is `failed`.
- It removes the worktree with `git worktree remove` (without `--force`), then deletes the branch and its `branch.<name>` config.
- It only touches local branches and worktrees. It never deletes anything on GitHub.
- It prints the tip commit of each deleted branch and a command to recreate it, such as `git branch feat-x 4f1c0de`. This works until git garbage-collects the commit.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The command ran. For `report`, this is true whatever the verdicts are. |
| `1` | The tool couldn't run (not a git repository, `gh` missing or not logged in, fetch failed, origin's default branch not fetched), a target doesn't exist, or `clean` refused or failed to delete a target. |
