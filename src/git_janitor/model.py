"""What git-janitor knows about a repository's branches and worktrees, and what it concludes."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

# Facts gathered from git and GitHub


@dataclass(frozen=True, slots=True)
class Branch:
    name: str
    tip: str
    upstream: str | None
    upstream_gone: bool


@dataclass(frozen=True, slots=True)
class Worktree:
    path: Path
    head: str
    branch: str | None
    is_main: bool
    locked: bool
    prunable: bool

    @property
    def detached(self) -> bool:
        return self.branch is None


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    subject: str


@dataclass(frozen=True, slots=True)
class Stash:
    ref: str
    subject: str
    branch: str | None
    """The branch the stash was made on, if any."""


@dataclass(frozen=True, slots=True)
class PreciousFile:
    path: str
    """Path relative to the worktree root."""
    size: int
    modified: datetime


class PullRequestState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    MERGED = "MERGED"


@dataclass(frozen=True, slots=True)
class PullRequest:
    number: int
    url: str
    state: PullRequestState
    base: str
    head_name: str
    head_oid: str


# Conclusions


class Status(StrEnum):
    MERGED = "merged"
    MERGED_PLUS_LOCAL = "merged_plus_local"
    PR_OPEN = "pr_open"
    PR_CLOSED = "pr_closed"
    MERGED_INTO_OTHER_BASE = "merged_into_other_base"
    NO_UNIQUE_COMMITS = "no_unique_commits"
    NO_PR = "no_pr"
    DEFAULT_BRANCH = "default_branch"


class Verdict(StrEnum):
    DELETE = "delete"
    KEEP = "keep"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class PullRequestEvidence:
    pull_request: PullRequest
    matched: bool


@dataclass(frozen=True, slots=True)
class ExtraCommits:
    """Local commits beyond the head of a pull request merged into the default branch."""

    pull_request: int
    commits: tuple[Commit, ...]


type Evidence = PullRequestEvidence | ExtraCommits


class BlockerCode(StrEnum):
    DIRTY = "dirty"
    UNTRACKED = "untracked"
    PRECIOUS_FILES = "precious_files"
    LOCKED = "locked"
    IN_PROGRESS_OP = "in_progress_op"
    IS_CURRENT = "is_current"
    IS_MAIN = "is_main"
    STASHES = "stashes"


@dataclass(frozen=True, slots=True)
class DirtyBlocker:
    code: ClassVar[BlockerCode] = BlockerCode.DIRTY
    paths: tuple[str, ...]

    @property
    def detail(self) -> str:
        return f"{len(self.paths)} modified or staged files"


@dataclass(frozen=True, slots=True)
class UntrackedBlocker:
    code: ClassVar[BlockerCode] = BlockerCode.UNTRACKED
    paths: tuple[str, ...]

    @property
    def detail(self) -> str:
        return f"{len(self.paths)} untracked files"


@dataclass(frozen=True, slots=True)
class PreciousFilesBlocker:
    code: ClassVar[BlockerCode] = BlockerCode.PRECIOUS_FILES
    files: tuple[PreciousFile, ...]

    @property
    def detail(self) -> str:
        return f"{len(self.files)} precious ignored files"


@dataclass(frozen=True, slots=True)
class LockedBlocker:
    code: ClassVar[BlockerCode] = BlockerCode.LOCKED
    detail: ClassVar[str] = "the worktree is locked"


@dataclass(frozen=True, slots=True)
class InProgressOpBlocker:
    code: ClassVar[BlockerCode] = BlockerCode.IN_PROGRESS_OP
    operation: str
    """merge, rebase, cherry-pick, revert, or bisect."""

    @property
    def detail(self) -> str:
        return f"{self.operation} in progress"


@dataclass(frozen=True, slots=True)
class IsCurrentBlocker:
    code: ClassVar[BlockerCode] = BlockerCode.IS_CURRENT
    detail: ClassVar[str] = "the current directory is in this worktree"


@dataclass(frozen=True, slots=True)
class IsMainBlocker:
    code: ClassVar[BlockerCode] = BlockerCode.IS_MAIN
    detail: ClassVar[str] = "checked out in the main worktree"


@dataclass(frozen=True, slots=True)
class StashesBlocker:
    code: ClassVar[BlockerCode] = BlockerCode.STASHES
    stashes: tuple[Stash, ...]

    @property
    def detail(self) -> str:
        return f"{len(self.stashes)} stash entries made on this branch"


type Blocker = (
    DirtyBlocker
    | UntrackedBlocker
    | PreciousFilesBlocker
    | LockedBlocker
    | InProgressOpBlocker
    | IsCurrentBlocker
    | IsMainBlocker
    | StashesBlocker
)


@dataclass(frozen=True, slots=True)
class RemoveWorktree:
    path: Path


@dataclass(frozen=True, slots=True)
class DeleteBranch:
    name: str
    tip: str


type Action = RemoveWorktree | DeleteBranch


@dataclass(frozen=True, slots=True)
class Candidate:
    """A branch, a branch and its worktree, or a detached worktree that could be cleaned up."""

    id: str
    status: Status
    branch: Branch | None
    worktree: Worktree | None
    blockers: tuple[Blocker, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    actions: tuple[Action, ...] = ()

    @property
    def verdict(self) -> Verdict:
        if self.status is Status.PR_OPEN:
            return Verdict.KEEP
        if self.status is Status.DEFAULT_BRANCH and not self.actions:
            return Verdict.KEEP
        if self.status is Status.MERGED and not self.blockers:
            return Verdict.DELETE
        return Verdict.REVIEW


@dataclass(frozen=True, slots=True)
class Report:
    repo: Path
    github_repo: str
    default_branch: str
    fetched: bool
    candidates: tuple[Candidate, ...]

    def candidate(self, id: str) -> Candidate:
        for candidate in self.candidates:
            if candidate.id == id:
                return candidate
        raise KeyError(id)

    def find(self, target: str, cwd: Path) -> Candidate | None:
        """Find a candidate by id, or by a worktree path relative to `cwd`."""
        path = (cwd / target).resolve()
        for candidate in self.candidates:
            if candidate.id == target:
                return candidate
            if candidate.worktree and candidate.worktree.path.resolve() == path:
                return candidate
        return None
