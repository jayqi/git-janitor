from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from git_janitor import git
from git_janitor.github import GitHubClient, PullRequest, PullRequestState
from git_janitor.precious import PreciousFile, find_precious_files

REMOTE = "origin"


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


@dataclass(frozen=True)
class DeleteBranch:
    name: str
    tip: str


@dataclass(frozen=True)
class RemoveWorktree:
    path: Path


type Action = RemoveWorktree | DeleteBranch


@dataclass(frozen=True)
class PullRequestEvidence:
    pull_request: PullRequest
    matched: bool


@dataclass(frozen=True)
class ExtraCommits:
    """Local commits beyond the head of a pull request merged into the default branch."""

    pull_request: int
    commits: tuple[git.Commit, ...]


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


@dataclass(frozen=True)
class Blocker:
    code: BlockerCode
    detail: str
    paths: tuple[str, ...] = ()
    files: tuple[PreciousFile, ...] = ()
    stashes: tuple[git.Stash, ...] = ()


@dataclass
class Candidate:
    id: str
    status: Status
    verdict: Verdict
    branch: git.Branch | None
    worktree: git.Worktree | None = None
    blockers: list[Blocker] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)


@dataclass
class Report:
    repo: Path
    github_repo: str
    default_branch: str
    fetched: bool
    candidates: list[Candidate]

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


def analyze(
    repo: Path, github: GitHubClient, *, fetch: bool = True, cwd: Path | None = None
) -> Report:
    """Analyze the local branches and worktrees of `repo`.

    `cwd` is the caller's current directory, used to detect worktrees that are in use.
    """
    cwd = (cwd or Path.cwd()).resolve()
    if fetch:
        git.fetch(REMOTE, cwd=repo)
    branches = git.list_branches(repo)
    worktrees = git.list_worktrees(repo)
    worktrees_by_branch = {wt.branch: wt for wt in worktrees if wt.branch}
    stashes = git.list_stashes(repo)
    head_names = sorted({name for branch in branches for name in _head_names(branch)})
    data = github.fetch(git.remote_url(REMOTE, cwd=repo), head_names)
    default_branch = data.default_branch
    default_ref = f"refs/remotes/{REMOTE}/{default_branch}"

    candidates = []
    for branch in branches:
        actions: list[Action]
        if branch.name == default_branch:
            status, evidence = Status.DEFAULT_BRANCH, []
            actions = []
        else:
            prs = [
                pr
                for name in _head_names(branch) - {default_branch}
                for pr in data.pull_requests.get(name, [])
            ]
            status, evidence = _classify_branch(branch, prs, default_ref, default_branch, repo)
            actions = [DeleteBranch(name=branch.name, tip=branch.tip)]
        worktree = worktrees_by_branch.get(branch.name)
        blockers = []
        if worktree and worktree.is_main:
            if status is not Status.DEFAULT_BRANCH:
                blockers.append(
                    Blocker(code=BlockerCode.IS_MAIN, detail="checked out in the main worktree")
                )
        elif worktree:
            actions.insert(0, RemoveWorktree(path=worktree.path))
            blockers.extend(_worktree_blockers(worktree, cwd))
        branch_stashes = tuple(stash for stash in stashes if stash.branch == branch.name)
        if branch_stashes:
            blockers.append(
                Blocker(
                    code=BlockerCode.STASHES,
                    detail=f"{len(branch_stashes)} stash entries made on this branch",
                    stashes=branch_stashes,
                )
            )
        candidates.append(
            Candidate(
                id=branch.name,
                status=status,
                verdict=_verdict(status, blockers, actions),
                branch=branch,
                worktree=worktree,
                blockers=blockers,
                evidence=evidence,
                actions=actions,
            )
        )

    for worktree in worktrees:
        if worktree.branch or worktree.is_main:
            continue
        status = _classify_unmatched(worktree.head, default_ref, repo)
        blockers = _worktree_blockers(worktree, cwd)
        actions: list[Action] = [RemoveWorktree(path=worktree.path)]
        candidates.append(
            Candidate(
                id=str(worktree.path),
                status=status,
                verdict=_verdict(status, blockers, actions),
                branch=None,
                worktree=worktree,
                blockers=blockers,
                actions=actions,
            )
        )

    return Report(
        repo=repo,
        github_repo=data.name_with_owner,
        default_branch=default_branch,
        fetched=fetch,
        candidates=candidates,
    )


def _verdict(status: Status, blockers: list[Blocker], actions: list[Action]) -> Verdict:
    if status is Status.PR_OPEN or (status is Status.DEFAULT_BRANCH and not actions):
        return Verdict.KEEP
    if status is Status.MERGED and not blockers:
        return Verdict.DELETE
    return Verdict.REVIEW


def _worktree_blockers(worktree: git.Worktree, cwd: Path) -> list[Blocker]:
    blockers = []
    if worktree.prunable:
        return blockers
    if cwd.is_relative_to(worktree.path.resolve()):
        blockers.append(
            Blocker(
                code=BlockerCode.IS_CURRENT, detail="the current directory is in this worktree"
            )
        )
    if worktree.locked:
        blockers.append(Blocker(code=BlockerCode.LOCKED, detail="the worktree is locked"))
    for operation in git.operations_in_progress(worktree.path):
        blockers.append(
            Blocker(code=BlockerCode.IN_PROGRESS_OP, detail=f"{operation} in progress")
        )
    status = git.worktree_status(worktree.path)
    if status.changed:
        blockers.append(
            Blocker(
                code=BlockerCode.DIRTY,
                detail=f"{len(status.changed)} modified or staged files",
                paths=tuple(status.changed),
            )
        )
    if status.untracked:
        blockers.append(
            Blocker(
                code=BlockerCode.UNTRACKED,
                detail=f"{len(status.untracked)} untracked files",
                paths=tuple(status.untracked),
            )
        )
    precious = find_precious_files(worktree.path, status.ignored)
    if precious:
        blockers.append(
            Blocker(
                code=BlockerCode.PRECIOUS_FILES,
                detail=f"{len(precious)} precious ignored files",
                files=tuple(precious),
            )
        )
    return blockers


def _head_names(branch: git.Branch) -> set[str]:
    """Names a pull request's head branch could have: the local name and the upstream's name."""
    names = {branch.name}
    if branch.upstream and branch.upstream.startswith(f"{REMOTE}/"):
        names.add(branch.upstream.removeprefix(f"{REMOTE}/"))
    return names


def _classify_branch(
    branch: git.Branch,
    prs: list[PullRequest],
    default_ref: str,
    default_branch: str,
    repo: Path,
) -> tuple[Status, list[Evidence]]:
    matched = {pr.number for pr in prs if _contains(pr, branch.tip, repo)}
    evidence: list[Evidence] = [
        PullRequestEvidence(pull_request=pr, matched=pr.number in matched)
        for pr in sorted(prs, key=lambda pr: pr.number, reverse=True)
    ]
    matching = [pr for pr in prs if pr.number in matched]
    if matching:
        pr = max(matching, key=lambda pr: pr.number)
        match pr.state:
            case PullRequestState.MERGED if pr.base == default_branch:
                return Status.MERGED, evidence
            case PullRequestState.MERGED:
                return Status.MERGED_INTO_OTHER_BASE, evidence
            case PullRequestState.OPEN:
                return Status.PR_OPEN, evidence
            case PullRequestState.CLOSED:
                return Status.PR_CLOSED, evidence

    merged_behind = [
        pr
        for pr in prs
        if pr.state is PullRequestState.MERGED
        and pr.base == default_branch
        and git.is_ancestor(pr.head_oid, branch.tip, cwd=repo)
    ]
    if merged_behind:
        pr = max(merged_behind, key=lambda pr: pr.number)
        extra = git.commits_between(pr.head_oid, branch.tip, cwd=repo)
        evidence.append(ExtraCommits(pull_request=pr.number, commits=tuple(extra)))
        return Status.MERGED_PLUS_LOCAL, evidence

    return _classify_unmatched(branch.tip, default_ref, repo), evidence


def _classify_unmatched(tip: str, default_ref: str, repo: Path) -> Status:
    """Status of a tip that no pull request accounts for."""
    if git.is_ancestor(tip, default_ref, cwd=repo):
        return Status.NO_UNIQUE_COMMITS
    return Status.NO_PR


def _contains(pr: PullRequest, tip: str, repo: Path) -> bool:
    """Whether `tip` is the pull request's head commit or one of its ancestors."""
    if pr.head_oid == tip:
        return True
    if not git.has_commit(pr.head_oid, cwd=repo):
        git.fetch_objects(REMOTE, f"refs/pull/{pr.number}/head", cwd=repo)
    return git.is_ancestor(tip, pr.head_oid, cwd=repo)
