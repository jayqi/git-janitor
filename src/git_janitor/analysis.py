from pathlib import Path

from git_janitor import git
from git_janitor.github import GitHubClient
from git_janitor.model import (
    Action,
    Blocker,
    Branch,
    BrokenWorktreeBlocker,
    Candidate,
    DeleteBranch,
    DirtyBlocker,
    Evidence,
    ExtraCommits,
    HiddenChangesBlocker,
    InProgressOpBlocker,
    IsCurrentBlocker,
    IsMainBlocker,
    LockedBlocker,
    MultipleWorktreesBlocker,
    NestedRepositoryBlocker,
    NestedWorktreeBlocker,
    PreciousFilesBlocker,
    PullRequest,
    PullRequestEvidence,
    PullRequestState,
    RemoveWorktree,
    Report,
    Stash,
    StashesBlocker,
    Status,
    SubmodulesBlocker,
    UnreadableBlocker,
    UntrackedBlocker,
    Worktree,
)
from git_janitor.precious import scan_ignored

REMOTE = "origin"


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
    worktrees_by_branch: dict[str, list[Worktree]] = {}
    for worktree in worktrees:
        if worktree.branch:
            worktrees_by_branch.setdefault(worktree.branch, []).append(worktree)
    stashes = git.list_stashes(repo)
    head_names = sorted({name for branch in branches for name in _head_names(branch)})
    data = github.fetch(git.remote_url(REMOTE, cwd=repo), head_names)
    default_branch = data.default_branch
    default_ref = f"refs/remotes/{REMOTE}/{default_branch}"
    if not git.ref_exists(default_ref, cwd=repo):
        raise git.GitError(
            f"{default_ref} doesn't exist. git-janitor needs {REMOTE}'s default branch "
            f"({default_branch}) to be fetched."
        )

    candidates = []
    for branch in branches:
        if branch.name == default_branch:
            status, evidence = Status.DEFAULT_BRANCH, ()
        else:
            prs = [
                pr
                for name in _head_names(branch) - {default_branch}
                for pr in data.pull_requests.get(name, [])
            ]
            status, evidence = _classify_branch(branch, prs, default_ref, default_branch, repo)
        branch_worktrees = worktrees_by_branch.get(branch.name, [])
        worktree = branch_worktrees[0] if branch_worktrees else None
        blockers = _branch_blockers(branch, status, worktree, worktrees, stashes, cwd)
        if len(branch_worktrees) > 1:
            paths = tuple(str(wt.path) for wt in branch_worktrees)
            blockers = (*blockers, MultipleWorktreesBlocker(paths=paths))
        candidates.append(
            Candidate(
                id=branch.name,
                status=status,
                branch=branch,
                worktree=worktree,
                blockers=blockers,
                evidence=evidence,
                actions=_branch_actions(branch, status, worktree),
            )
        )

    branch_names = {branch.name for branch in branches}
    for worktree in worktrees:
        if worktree.is_main or worktree.branch in branch_names:
            continue
        if worktree.branch is None:
            status = _classify_unmatched(worktree.head, default_ref, repo)
        else:
            status = Status.BRANCH_MISSING
        candidates.append(
            Candidate(
                id=str(worktree.path),
                status=status,
                branch=None,
                worktree=worktree,
                blockers=worktree_blockers(worktree, worktrees, cwd),
                actions=(RemoveWorktree(path=worktree.path),),
            )
        )

    return Report(
        repo=next(wt.path for wt in worktrees if wt.is_main),
        github_repo=data.name_with_owner,
        default_branch=default_branch,
        fetched=fetch,
        candidates=tuple(candidates),
    )


def _branch_actions(
    branch: Branch, status: Status, worktree: Worktree | None
) -> tuple[Action, ...]:
    actions: list[Action] = []
    if worktree and not worktree.is_main:
        actions.append(RemoveWorktree(path=worktree.path))
    if status is not Status.DEFAULT_BRANCH:
        actions.append(DeleteBranch(name=branch.name, tip=branch.tip))
    return tuple(actions)


def _branch_blockers(
    branch: Branch,
    status: Status,
    worktree: Worktree | None,
    worktrees: list[Worktree],
    stashes: list[Stash],
    cwd: Path,
) -> tuple[Blocker, ...]:
    blockers: list[Blocker] = []
    if worktree and worktree.is_main:
        if status is not Status.DEFAULT_BRANCH:
            blockers.append(IsMainBlocker())
    elif worktree:
        blockers.extend(worktree_blockers(worktree, worktrees, cwd))
    branch_stashes = tuple(stash for stash in stashes if stash.branch == branch.name)
    if branch_stashes:
        blockers.append(StashesBlocker(stashes=branch_stashes))
    return tuple(blockers)


def worktree_blockers(
    worktree: Worktree, worktrees: list[Worktree], cwd: Path
) -> tuple[Blocker, ...]:
    """Blockers for removing a linked worktree. `worktrees` is every worktree of the repo."""
    if not worktree.path.exists():
        return (LockedBlocker(),) if worktree.locked else ()
    if worktree.prunable:
        return (BrokenWorktreeBlocker(),)
    blockers: list[Blocker] = []
    if cwd.is_relative_to(worktree.path.resolve()):
        blockers.append(IsCurrentBlocker())
    if worktree.locked:
        blockers.append(LockedBlocker())
    for operation in git.operations_in_progress(worktree.path):
        blockers.append(InProgressOpBlocker(operation=operation))
    status = git.worktree_status(worktree.path)
    if status.changed:
        blockers.append(DirtyBlocker(paths=status.changed))
    if hidden := git.hidden_changes(worktree.path):
        blockers.append(HiddenChangesBlocker(paths=hidden))
    if status.untracked:
        blockers.append(UntrackedBlocker(paths=status.untracked))
    if submodules := git.populated_submodules(worktree.path):
        blockers.append(SubmodulesBlocker(paths=submodules))
    nested = [
        other.path
        for other in worktrees
        if other.path != worktree.path and other.path.is_relative_to(worktree.path)
    ]
    if nested:
        paths = tuple(path.relative_to(worktree.path).as_posix() for path in nested)
        blockers.append(NestedWorktreeBlocker(paths=paths))
    scan = scan_ignored(worktree.path, status.ignored, skip=nested)
    if scan.nested_repositories:
        blockers.append(NestedRepositoryBlocker(paths=scan.nested_repositories))
    if scan.precious_files:
        blockers.append(PreciousFilesBlocker(files=scan.precious_files))
    if scan.unreadable:
        blockers.append(UnreadableBlocker(paths=scan.unreadable))
    return tuple(blockers)


def _head_names(branch: Branch) -> set[str]:
    """Names a pull request's head branch could have: the local name and the upstream's name."""
    names = {branch.name}
    if branch.upstream_remote == REMOTE and branch.upstream_branch:
        names.add(branch.upstream_branch)
    return names


def _classify_branch(
    branch: Branch,
    prs: list[PullRequest],
    default_ref: str,
    default_branch: str,
    repo: Path,
) -> tuple[Status, tuple[Evidence, ...]]:
    in_default = git.is_ancestor(branch.tip, default_ref, cwd=repo)
    matched = {pr.number for pr in prs if _contains(pr, branch.tip, in_default, repo)}
    pr_evidence = tuple(
        PullRequestEvidence(pull_request=pr, matched=pr.number in matched)
        for pr in sorted(prs, key=lambda pr: pr.number, reverse=True)
    )
    matching = [pr for pr in prs if pr.number in matched]
    if any(pr.state is PullRequestState.OPEN for pr in matching):
        return Status.PR_OPEN, pr_evidence
    if matching:
        pr = max(matching, key=lambda pr: pr.number)
        match pr.state:
            case PullRequestState.MERGED if pr.base == default_branch:
                return Status.MERGED, pr_evidence
            case PullRequestState.MERGED:
                return Status.MERGED_INTO_OTHER_BASE, pr_evidence
            case _:
                return Status.PR_CLOSED, pr_evidence
    if in_default:
        return Status.NO_UNIQUE_COMMITS, pr_evidence

    # Pull requests whose head the tip has moved beyond
    behind = [
        pr
        for pr in prs
        if (
            pr.state is PullRequestState.OPEN
            or (pr.state is PullRequestState.MERGED and pr.base == default_branch)
        )
        and git.has_commit(pr.head_oid, cwd=repo)
        and git.is_ancestor(pr.head_oid, branch.tip, cwd=repo)
    ]
    if behind:
        open_prs = [pr for pr in behind if pr.state is PullRequestState.OPEN]
        pr = max(open_prs or behind, key=lambda pr: pr.number)
        extra = ExtraCommits(
            pull_request=pr.number,
            commits=tuple(
                git.commits_between(pr.head_oid, branch.tip, cwd=repo, exclude=default_ref)
            ),
        )
        status = Status.PR_OPEN if open_prs else Status.MERGED_PLUS_LOCAL
        return status, (*pr_evidence, extra)

    return Status.NO_PR, pr_evidence


def _classify_unmatched(tip: str, default_ref: str, repo: Path) -> Status:
    """Status of a tip that no pull request accounts for."""
    if git.is_ancestor(tip, default_ref, cwd=repo):
        return Status.NO_UNIQUE_COMMITS
    return Status.NO_PR


def _contains(pr: PullRequest, tip: str, in_default: bool, repo: Path) -> bool:
    """Whether `tip` is the pull request's head commit or one of the pull request's own commits.

    A tip already in the default branch only matches the head exactly. Otherwise, a branch
    created from history that predates the pull request would match it.
    """
    if pr.head_oid == tip:
        return True
    if in_default:
        return False
    if not git.has_commit(pr.head_oid, cwd=repo):
        try:
            git.fetch_objects(REMOTE, f"refs/pull/{pr.number}/head", cwd=repo)
        except git.GitError:
            return False
    return git.is_ancestor(tip, pr.head_oid, cwd=repo)
