"""What git-janitor knows about a repository's branches and worktrees, and what it concludes."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

# Facts gathered from git and GitHub


@dataclass(frozen=True, slots=True)
class Branch:
    name: str
    tip: str
    upstream_remote: str | None
    upstream_branch: str | None
    upstream_gone: bool

    @property
    def upstream(self) -> str | None:
        """The upstream as `<remote>/<branch>`."""
        if self.upstream_remote and self.upstream_branch:
            return f"{self.upstream_remote}/{self.upstream_branch}"
        return None


@dataclass(frozen=True, slots=True)
class Worktree:
    path: Path
    head: str
    branch: str | None
    is_main: bool
    locked: bool
    prunable: bool
    admin_dir: Path | None = None
    """Git's administrative directory for a linked worktree, `.git/worktrees/<id>`."""

    @property
    def detached(self) -> bool:
        return self.branch is None


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    subject: str


@dataclass(frozen=True, slots=True)
class PreciousEntry:
    """An ignored file or directory, as `git status` lists it, holding precious files."""

    path: str
    """Path relative to the worktree root. A directory's ends in `/`."""
    files: int
    size: int
    """Total size of the precious files, in bytes."""
    modified: datetime
    """When the most recently modified precious file was modified."""


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
    BRANCH_MISSING = "branch_missing"


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
    NESTED_REPOSITORY = "nested_repository"
    UNREADABLE = "unreadable"
    RECENT_ACTIVITY = "recent_activity"
    ERROR = "error"


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
    entries: tuple[PreciousEntry, ...]

    @property
    def detail(self) -> str:
        return f"{sum(e.files for e in self.entries)} precious ignored files"


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
class NestedRepositoryBlocker:
    code: ClassVar[BlockerCode] = BlockerCode.NESTED_REPOSITORY
    paths: tuple[str, ...]
    """Roots of git repositories inside ignored directories."""

    @property
    def detail(self) -> str:
        return f"{len(self.paths)} git repositories inside ignored directories"


@dataclass(frozen=True, slots=True)
class UnreadableBlocker:
    code: ClassVar[BlockerCode] = BlockerCode.UNREADABLE
    paths: tuple[str, ...]
    """Ignored paths that couldn't be read to check whether they're precious."""

    @property
    def detail(self) -> str:
        return f"{len(self.paths)} ignored paths couldn't be read"


@dataclass(frozen=True, slots=True)
class RecentActivityBlocker:
    code: ClassVar[BlockerCode] = BlockerCode.RECENT_ACTIVITY
    min_age: timedelta

    @property
    def detail(self) -> str:
        return f"active within the last {format_duration(self.min_age)} (janitor.minAge)"


@dataclass(frozen=True, slots=True)
class ErrorBlocker:
    """Checking the worktree failed, so whether anything would be lost is unknown."""

    code: ClassVar[BlockerCode] = BlockerCode.ERROR
    detail: str


type Blocker = (
    DirtyBlocker
    | UntrackedBlocker
    | PreciousFilesBlocker
    | LockedBlocker
    | InProgressOpBlocker
    | IsCurrentBlocker
    | IsMainBlocker
    | NestedRepositoryBlocker
    | UnreadableBlocker
    | RecentActivityBlocker
    | ErrorBlocker
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
    last_activity: datetime | None = None
    """When the branch or worktree last changed, from git's reflogs and worktree files."""

    @property
    def verdict(self) -> Verdict:
        return self.verdict_accepting(())

    def verdict_accepting(self, accepted: Collection[BlockerCode]) -> Verdict:
        """The verdict if blockers with the `accepted` codes are ignored."""
        if self.status in (Status.PR_OPEN, Status.DEFAULT_BRANCH):
            return Verdict.KEEP
        if self.status in DELETABLE_STATUSES and not self.unaccepted_blockers(accepted):
            return Verdict.DELETE
        return Verdict.REVIEW

    def unaccepted_blockers(self, accepted: Collection[BlockerCode]) -> tuple[Blocker, ...]:
        return tuple(b for b in self.blockers if b.code not in accepted)


DELETABLE_STATUSES = frozenset({Status.MERGED, Status.NO_UNIQUE_COMMITS})
"""Statuses where deleting loses no commits."""

ACCEPTABLE_BLOCKERS = frozenset({BlockerCode.PRECIOUS_FILES, BlockerCode.RECENT_ACTIVITY})
"""Blockers that `clean` can be told to accept: precious files, which the blocker lists, and
recent activity, which only protects worktrees that may be in use.
"""


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
        """Find a candidate by id, or else by a worktree path relative to `cwd`."""
        for candidate in self.candidates:
            if candidate.id == target:
                return candidate
        path = (cwd / target).resolve()
        for candidate in self.candidates:
            if candidate.worktree and candidate.worktree.path.resolve() == path:
                return candidate
        return None


class Outcome(StrEnum):
    DELETED = "deleted"
    WOULD_DELETE = "would_delete"
    REFUSED = "refused"
    NOT_FOUND = "not_found"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TargetOutcome:
    """The result of cleaning one target."""

    id: str
    outcome: Outcome
    verdict: Verdict | None = None
    detail: str = ""
    actions: tuple[Action, ...] = ()
    """The candidate's planned actions."""
    recovery: tuple[str, ...] = ()
    """Commands that restore what the completed actions made unreachable."""
    accepted: tuple[Blocker, ...] = ()
    """The candidate's blockers that were accepted, for targets that weren't refused."""


@dataclass(frozen=True, slots=True)
class CleanResult:
    report: Report
    outcomes: tuple[TargetOutcome, ...]

    @property
    def ok(self) -> bool:
        return all(o.outcome in (Outcome.DELETED, Outcome.WOULD_DELETE) for o in self.outcomes)


DURATION_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(text: str) -> timedelta:
    """Parse a duration like `7d`, `12h`, `30m`, `45s`, or `0`."""
    text = text.strip()
    if text == "0":
        return timedelta(0)
    number, unit = text[:-1], text[-1:]
    if not number.isdigit() or unit not in DURATION_UNITS:
        raise ValueError(f"Invalid duration {text!r}; expected something like 7d, 12h, or 30m.")
    return timedelta(seconds=int(number) * DURATION_UNITS[unit])


def format_duration(duration: timedelta) -> str:
    seconds = int(duration.total_seconds())
    for unit, size in DURATION_UNITS.items():
        if seconds and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return "0"
