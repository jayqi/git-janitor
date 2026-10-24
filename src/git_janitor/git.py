from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess


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


@dataclass(frozen=True)
class Branch:
    name: str
    tip: str
    upstream: str | None
    upstream_gone: bool


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


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str


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


@dataclass(frozen=True)
class Worktree:
    path: Path
    head: str
    branch: str | None
    is_main: bool
    locked: bool
    prunable: bool


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


@dataclass(frozen=True)
class WorktreeStatus:
    changed: list[str]
    untracked: list[str]
    ignored: list[str]
    """Ignored paths. A wholly ignored directory is one entry ending in `/`."""


def worktree_status(cwd: Path) -> WorktreeStatus:
    output = run_git(
        "status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignored=matching", cwd=cwd
    )
    status = WorktreeStatus(changed=[], untracked=[], ignored=[])
    tokens = iter(output.split("\0"))
    for token in tokens:
        match token[:1]:
            case "1":
                status.changed.append(token.split(" ", 8)[8])
            case "2":
                status.changed.append(token.split(" ", 9)[9])
                next(tokens)  # Original path of a rename or copy
            case "u":
                status.changed.append(token.split(" ", 10)[10])
            case "?":
                status.untracked.append(token[2:])
            case "!":
                status.ignored.append(token[2:])
    return status


def ignore_sources(paths: list[str], cwd: Path) -> dict[str, str]:
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


@dataclass(frozen=True)
class Stash:
    ref: str
    subject: str

    @property
    def branch(self) -> str | None:
        """The branch from a "WIP on <branch>: ..." or "On <branch>: ..." stash subject."""
        for prefix in ("WIP on ", "On "):
            if self.subject.startswith(prefix):
                return self.subject.removeprefix(prefix).split(": ", 1)[0]
        return None


def list_stashes(cwd: Path) -> list[Stash]:
    output = run_git("stash", "list", "--format=%gd%x00%gs", cwd=cwd)
    return [Stash(*line.split("\0", 1)) for line in output.splitlines()]
