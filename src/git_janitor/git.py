from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import subprocess

from git_janitor.model import Branch, Commit, Stash, Worktree


class GitError(RuntimeError):
    pass


def run_git(*args: str, cwd: Path, input: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, input=input, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def succeeds(*args: str, cwd: Path) -> bool:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False)
    return result.returncode == 0


def list_branches(cwd: Path) -> list[Branch]:
    output = run_git(
        "for-each-ref",
        "--format=%(refname:short)%00%(objectname)%00%(upstream:short)%00%(upstream:track)",
        "refs/heads",
        cwd=cwd,
    )
    branches = []
    for line in output.splitlines():
        name, tip, upstream, track = line.split("\0")
        branches.append(
            Branch(
                name=name,
                tip=tip,
                upstream=upstream or None,
                upstream_gone=track == "[gone]",
            )
        )
    return branches


def is_ancestor(ancestor: str, descendant: str, cwd: Path) -> bool:
    return succeeds("merge-base", "--is-ancestor", ancestor, descendant, cwd=cwd)


def commits_between(base: str, tip: str, cwd: Path) -> list[Commit]:
    """Commits reachable from `tip` but not `base`, oldest first."""
    output = run_git("log", "--reverse", "--format=%H%x00%s", f"{base}..{tip}", cwd=cwd)
    return [Commit(*line.split("\0", 1)) for line in output.splitlines()]


def has_commit(sha: str, cwd: Path) -> bool:
    return succeeds("cat-file", "-e", f"{sha}^{{commit}}", cwd=cwd)


def fetch_objects(remote: str, ref: str, cwd: Path) -> None:
    """Fetch the objects for `ref` without creating any local refs."""
    run_git("fetch", "--quiet", "--no-write-fetch-head", remote, ref, cwd=cwd)


def remote_url(remote: str, cwd: Path) -> str:
    return run_git("remote", "get-url", remote, cwd=cwd).strip()


def fetch(remote: str, cwd: Path) -> None:
    run_git("fetch", "--prune", remote, cwd=cwd)


def list_worktrees(cwd: Path) -> list[Worktree]:
    output = run_git("worktree", "list", "--porcelain", "-z", cwd=cwd)
    worktrees = []
    for record in output.split("\0\0"):
        if not record.strip("\0"):
            continue
        fields = {}
        for line in record.split("\0"):
            key, _, value = line.partition(" ")
            fields[key] = value
        branch = fields.get("branch")
        worktrees.append(
            Worktree(
                path=Path(fields["worktree"]),
                head=fields.get("HEAD", ""),
                branch=branch.removeprefix("refs/heads/") if branch else None,
                is_main=not worktrees,
                locked="locked" in fields,
                prunable="prunable" in fields,
            )
        )
    return worktrees


@dataclass(frozen=True, slots=True)
class WorktreeStatus:
    changed: tuple[str, ...]
    untracked: tuple[str, ...]
    ignored: tuple[str, ...]
    """Ignored paths. A wholly ignored directory is one entry ending in `/`."""


def worktree_status(cwd: Path) -> WorktreeStatus:
    output = run_git(
        "status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignored=matching", cwd=cwd
    )
    changed, untracked, ignored = [], [], []
    tokens = iter(output.split("\0"))
    for token in tokens:
        match token[:1]:
            case "1":
                changed.append(token.split(" ", 8)[8])
            case "2":
                changed.append(token.split(" ", 9)[9])
                next(tokens)  # Original path of a rename or copy
            case "u":
                changed.append(token.split(" ", 10)[10])
            case "?":
                untracked.append(token[2:])
            case "!":
                ignored.append(token[2:])
    return WorktreeStatus(
        changed=tuple(changed), untracked=tuple(untracked), ignored=tuple(ignored)
    )


def ignore_sources(paths: Sequence[str], cwd: Path) -> dict[str, str]:
    """Map each ignored path to the ignore file whose rule matched it."""
    output = run_git("check-ignore", "-v", "-z", "--stdin", cwd=cwd, input="\0".join(paths))
    fields = output.split("\0")
    return {fields[i + 3]: fields[i] for i in range(0, len(fields) - 3, 4)}


def config_get_all(key: str, cwd: Path) -> list[str]:
    result = subprocess.run(
        ["git", "config", "--get-all", key], cwd=cwd, capture_output=True, text=True, check=False
    )
    return result.stdout.splitlines()


IN_PROGRESS_MARKERS = {
    "rebase-merge": "rebase",
    "rebase-apply": "rebase",
    "MERGE_HEAD": "merge",
    "CHERRY_PICK_HEAD": "cherry-pick",
    "REVERT_HEAD": "revert",
    "BISECT_LOG": "bisect",
}


def operations_in_progress(cwd: Path) -> list[str]:
    """Names of operations (merge, rebase, ...) in progress in the worktree at `cwd`."""
    args = [arg for marker in IN_PROGRESS_MARKERS for arg in ("--git-path", marker)]
    paths = run_git("rev-parse", *args, cwd=cwd).splitlines()
    operations = []
    for marker, path in zip(IN_PROGRESS_MARKERS, paths, strict=True):
        operation = IN_PROGRESS_MARKERS[marker]
        if (cwd / path).exists() and operation not in operations:
            operations.append(operation)
    return operations


def list_stashes(cwd: Path) -> list[Stash]:
    output = run_git("stash", "list", "--format=%gd%x00%gs", cwd=cwd)
    stashes = []
    for line in output.splitlines():
        ref, subject = line.split("\0", 1)
        stashes.append(Stash(ref=ref, subject=subject, branch=_stash_branch(subject)))
    return stashes


def _stash_branch(subject: str) -> str | None:
    """The branch from a "WIP on <branch>: ..." or "On <branch>: ..." stash subject."""
    for prefix in ("WIP on ", "On "):
        if subject.startswith(prefix):
            branch = subject.removeprefix(prefix).split(": ", 1)[0]
            return None if branch == "(no branch)" else branch
    return None
