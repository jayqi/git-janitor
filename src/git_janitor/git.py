from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import subprocess
from subprocess import CompletedProcess

from git_janitor.model import Branch, Commit, Stash, Worktree


class GitError(RuntimeError):
    pass


def _git(args: Sequence[str], cwd: Path, input: str | None = None) -> CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            input=input,
            capture_output=True,
            text=True,
            errors="surrogateescape",
            check=False,
        )
    except FileNotFoundError as exc:
        if not cwd.is_dir():
            raise GitError(f"Directory does not exist: {cwd}") from exc
        raise GitError("git is not installed.") from exc


def run_git(*args: str, cwd: Path, input: str | None = None) -> str:
    result = _git(args, cwd, input)
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def succeeds(*args: str, cwd: Path) -> bool:
    return _git(args, cwd).returncode == 0


def list_branches(cwd: Path) -> list[Branch]:
    output = run_git(
        "for-each-ref",
        "--format=%(refname:lstrip=2)%00%(objectname)%00%(upstream:remotename)"
        "%00%(upstream:remoteref)%00%(upstream:track)",
        "refs/heads",
        cwd=cwd,
    )
    branches = []
    for line in _lines(output):
        name, tip, remote, remote_ref, track = line.split("\0")
        has_remote = remote not in ("", ".")
        branches.append(
            Branch(
                name=name,
                tip=tip,
                upstream_remote=remote if has_remote else None,
                upstream_branch=remote_ref.removeprefix("refs/heads/") if has_remote else None,
                upstream_gone=track == "[gone]",
            )
        )
    return branches


def _lines(output: str) -> list[str]:
    """Split git output on newlines only, unlike `str.splitlines`."""
    return [line for line in output.split("\n") if line]


def is_ancestor(ancestor: str, descendant: str, cwd: Path) -> bool:
    result = _git(["merge-base", "--is-ancestor", ancestor, descendant], cwd)
    if result.returncode not in (0, 1):
        raise GitError(f"git merge-base --is-ancestor failed: {result.stderr.strip()}")
    return result.returncode == 0


def reflog_time(ref: str, cwd: Path) -> datetime | None:
    """When `ref`'s reflog was last written, if it has one."""
    result = _git(["log", "-g", "-1", "--date=unix", "--format=%gd", ref], cwd)
    selector = result.stdout.strip()
    if result.returncode != 0 or not selector.endswith("}"):
        return None
    return datetime.fromtimestamp(int(selector.rsplit("@{", 1)[1][:-1]), tz=UTC)


def worktree_activity(worktree: Worktree, repo: Path) -> datetime | None:
    """When a linked worktree was created or its HEAD last moved.

    Read from the worktree's admin directory, so it works when the worktree's directory is
    missing. Other files there, like the index, are rewritten by ordinary commands such as
    `git status`.
    """
    if worktree.admin_dir is None:
        return None
    times = [reflog_time(f"worktrees/{worktree.admin_dir.name}/HEAD", cwd=repo)]
    try:
        created = (worktree.admin_dir / "commondir").stat().st_mtime
        times.append(datetime.fromtimestamp(created, tz=UTC))
    except OSError:
        pass
    return max((t for t in times if t is not None), default=None)


def ref_exists(ref: str, cwd: Path) -> bool:
    return succeeds("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=cwd)


def commits_between(base: str, tip: str, cwd: Path, exclude: str | None = None) -> list[Commit]:
    """Commits reachable from `tip` but not from `base` or `exclude`, oldest first."""
    excluded = [f"^{base}"] + ([f"^{exclude}"] if exclude else [])
    output = run_git("log", "--reverse", "--format=%H%x00%s", tip, *excluded, "--", cwd=cwd)
    return [Commit(*line.split("\0", 1)) for line in _lines(output)]


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
    admin_dirs = _worktree_admin_dirs(cwd)
    worktrees = []
    for record in output.split("\0\0"):
        if not record.strip("\0"):
            continue
        fields = {}
        for line in record.split("\0"):
            key, _, value = line.partition(" ")
            fields[key] = value
        path = Path(fields["worktree"])
        branch = fields.get("branch")
        if branch:
            branch = branch.removeprefix("refs/heads/")
        elif "detached" in fields and "prunable" not in fields and path.is_dir():
            branch = _in_progress_branch(path)
        worktrees.append(
            Worktree(
                path=path,
                head=fields.get("HEAD", ""),
                branch=branch,
                is_main=not worktrees,
                locked="locked" in fields,
                prunable="prunable" in fields,
                admin_dir=admin_dirs.get(path.resolve()),
            )
        )
    return worktrees


def _worktree_admin_dirs(cwd: Path) -> dict[Path, Path]:
    """Map linked worktree paths to their admin directories."""
    common_dir = cwd / run_git("rev-parse", "--git-common-dir", cwd=cwd).strip()
    admin_dirs = {}
    for admin_dir in (common_dir / "worktrees").glob("*/"):
        try:
            dot_git = Path((admin_dir / "gitdir").read_text().strip())
        except OSError:
            continue
        admin_dirs[dot_git.parent.resolve()] = admin_dir.resolve()
    return admin_dirs


def _in_progress_branch(worktree: Path) -> str | None:
    """The branch being rebased or bisected in a worktree, which git lists as detached."""
    rebase_merge, rebase_apply, bisect_start = _lines(
        run_git(
            "rev-parse",
            "--git-path",
            "rebase-merge/head-name",
            "--git-path",
            "rebase-apply/head-name",
            "--git-path",
            "BISECT_START",
            cwd=worktree,
        )
    )
    for head_name in (worktree / rebase_merge, worktree / rebase_apply):
        if head_name.is_file():
            ref = head_name.read_text().strip()
            if ref.startswith("refs/heads/"):
                return ref.removeprefix("refs/heads/")
    start = worktree / bisect_start
    if start.is_file():
        name = start.read_text().strip()
        if succeeds("show-ref", "--verify", "--quiet", f"refs/heads/{name}", cwd=worktree):
            return name
    return None


@dataclass(frozen=True, slots=True)
class WorktreeStatus:
    changed: tuple[str, ...]
    untracked: tuple[str, ...]
    ignored: tuple[str, ...]
    """Ignored paths. A wholly ignored directory is one entry ending in `/`."""


def worktree_status(cwd: Path) -> WorktreeStatus:
    output = run_git(
        "--no-optional-locks",
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
        "--ignored=matching",
        cwd=cwd,
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
    # A `./` prefix stops check-ignore from reading paths like `:x` as pathspec magic
    result = _git(
        ["check-ignore", "-v", "-z", "--stdin"], cwd, input="\0".join(f"./{p}" for p in paths)
    )
    if result.returncode == 1:
        return {}
    if result.returncode != 0:
        raise GitError(f"git check-ignore failed: {result.stderr.strip()}")
    fields = result.stdout.split("\0")
    return {fields[i + 3].removeprefix("./"): fields[i] for i in range(0, len(fields) - 3, 4)}


def tracked_files(paths: Sequence[str], cwd: Path) -> set[str]:
    """The subset of `paths` that git tracks."""
    if not paths:
        return set()
    output = run_git("--literal-pathspecs", "ls-files", "-z", "--", *paths, cwd=cwd)
    return {path for path in output.split("\0") if path}


def config_get_all(key: str, cwd: Path) -> list[str]:
    return _lines(_git(["config", "--get-all", key], cwd).stdout)


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
    paths = _lines(run_git("rev-parse", *args, cwd=cwd))
    operations = []
    for marker, path in zip(IN_PROGRESS_MARKERS, paths, strict=True):
        operation = IN_PROGRESS_MARKERS[marker]
        if (cwd / path).exists() and operation not in operations:
            operations.append(operation)
    return operations


def list_stashes(cwd: Path) -> list[Stash]:
    output = run_git("stash", "list", "--format=%gd%x00%gs", cwd=cwd)
    stashes = []
    for line in _lines(output):
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
