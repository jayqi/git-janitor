from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import shlex

from git_janitor import git
from git_janitor.analysis import analyze, worktree_blockers
from git_janitor.github import GitHubClient
from git_janitor.model import Action, Candidate, DeleteBranch, RemoveWorktree, Report, Verdict


class Outcome(StrEnum):
    DELETED = "deleted"
    WOULD_DELETE = "would_delete"
    REFUSED = "refused"
    NOT_FOUND = "not_found"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TargetOutcome:
    id: str
    outcome: Outcome
    verdict: Verdict | None = None
    detail: str = ""
    actions: tuple[Action, ...] = ()
    """The candidate's planned actions."""
    recovery: tuple[str, ...] = ()
    """Commands that restore what the completed actions made unreachable."""


@dataclass(frozen=True, slots=True)
class CleanResult:
    report: Report
    outcomes: tuple[TargetOutcome, ...]

    @property
    def ok(self) -> bool:
        return all(o.outcome in (Outcome.DELETED, Outcome.WOULD_DELETE) for o in self.outcomes)


def clean(
    repo: Path,
    github: GitHubClient,
    *,
    targets: Sequence[str] = (),
    all: bool = False,
    dry_run: bool = False,
    fetch: bool = True,
    cwd: Path | None = None,
) -> CleanResult:
    """Re-analyze `repo` and carry out the actions of targets with a delete verdict.

    Each action first checks that its target hasn't changed since the analysis.
    """
    if bool(targets) == all:
        raise ValueError("Pass either targets or all=True.")
    cwd = (cwd or Path.cwd()).resolve()
    report = analyze(repo, github, fetch=fetch, cwd=cwd)

    if all:
        selected: list[Candidate | str] = [
            c for c in report.candidates if c.verdict is Verdict.DELETE
        ]
    else:
        selected = []
        for target in targets:
            candidate = report.find(target, cwd=cwd)
            if candidate is None:
                selected.append(target)
            elif candidate not in selected:
                selected.append(candidate)

    outcomes = [
        TargetOutcome(id=item, outcome=Outcome.NOT_FOUND, detail="no such candidate")
        if isinstance(item, str)
        else _clean_candidate(item, repo, cwd, dry_run=dry_run)
        for item in selected
    ]
    return CleanResult(report=report, outcomes=tuple(outcomes))


class ChangedSinceAnalysisError(Exception):
    pass


def _clean_candidate(
    candidate: Candidate, repo: Path, cwd: Path, *, dry_run: bool
) -> TargetOutcome:
    def outcome(outcome: Outcome, detail: str = "", done: Sequence[Action] = ()) -> TargetOutcome:
        return TargetOutcome(
            id=candidate.id,
            outcome=outcome,
            verdict=candidate.verdict,
            detail=detail,
            actions=candidate.actions,
            recovery=_recovery(done),
        )

    if candidate.verdict is not Verdict.DELETE:
        return outcome(Outcome.REFUSED, f"verdict is {candidate.verdict}, not delete")
    if dry_run:
        return outcome(Outcome.WOULD_DELETE, done=candidate.actions)

    done: list[Action] = []
    for action in candidate.actions:
        try:
            _execute(action, candidate, repo, cwd)
        except (git.GitError, ChangedSinceAnalysisError) as exc:
            return outcome(Outcome.FAILED, str(exc), done)
        done.append(action)
    return outcome(Outcome.DELETED, done=done)


def _execute(action: Action, candidate: Candidate, repo: Path, cwd: Path) -> None:
    worktrees = git.list_worktrees(repo)
    match action:
        case RemoveWorktree(path=path):
            current = next((wt for wt in worktrees if wt.path == path), None)
            if current is None or candidate.worktree is None:
                raise ChangedSinceAnalysisError(f"{path} is no longer a worktree")
            if current.head != candidate.worktree.head:
                raise ChangedSinceAnalysisError(f"HEAD of {path} changed since the analysis")
            blockers = worktree_blockers(current, worktrees, cwd)
            if blockers:
                codes = ", ".join(str(b.code) for b in blockers)
                raise ChangedSinceAnalysisError(f"{path} has new blockers: {codes}")
            git.run_git("worktree", "remove", str(path), cwd=repo)
        case DeleteBranch(name=name, tip=tip):
            if any(wt.branch == name for wt in worktrees):
                raise ChangedSinceAnalysisError(f"{name} is now checked out in a worktree")
            git.run_git("update-ref", "-d", f"refs/heads/{name}", tip, cwd=repo)
            git.succeeds("config", "--remove-section", f"branch.{name}", cwd=repo)


def _recovery(done: Sequence[Action]) -> tuple[str, ...]:
    """Commands that recreate the branches deleted by `done`."""
    return tuple(
        f"git branch {shlex.quote(action.name)} {action.tip}"
        for action in done
        if isinstance(action, DeleteBranch)
    )
