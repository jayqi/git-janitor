from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from git_janitor import git
from git_janitor.analysis import analyze
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
    """Re-analyze `repo` and carry out the actions of targets with a delete verdict."""
    if not targets and not all:
        raise ValueError("Pass targets or all=True.")
    cwd = (cwd or Path.cwd()).resolve()
    report = analyze(repo, github, fetch=fetch, cwd=cwd)

    if all:
        outcomes = [
            _clean_candidate(candidate, repo, dry_run=dry_run)
            for candidate in report.candidates
            if candidate.verdict is Verdict.DELETE
        ]
    else:
        outcomes = []
        for target in targets:
            candidate = report.find(target, cwd=cwd)
            if candidate is None:
                outcomes.append(
                    TargetOutcome(id=target, outcome=Outcome.NOT_FOUND, detail="no such candidate")
                )
            else:
                outcomes.append(_clean_candidate(candidate, repo, dry_run=dry_run))
    return CleanResult(report=report, outcomes=tuple(outcomes))


def _clean_candidate(candidate: Candidate, repo: Path, *, dry_run: bool) -> TargetOutcome:
    def outcome(outcome: Outcome, detail: str = "", done: Sequence[Action] = ()) -> TargetOutcome:
        return TargetOutcome(
            id=candidate.id,
            outcome=outcome,
            verdict=candidate.verdict,
            detail=detail,
            actions=candidate.actions,
            recovery=_recovery(candidate, done),
        )

    if candidate.verdict is not Verdict.DELETE:
        return outcome(Outcome.REFUSED, f"verdict is {candidate.verdict}, not delete")
    if dry_run:
        return outcome(Outcome.WOULD_DELETE, done=candidate.actions)

    done: list[Action] = []
    for action in candidate.actions:
        try:
            _execute(action, repo)
        except git.GitError as exc:
            return outcome(Outcome.FAILED, str(exc), done)
        done.append(action)
    return outcome(Outcome.DELETED, done=done)


def _execute(action: Action, repo: Path) -> None:
    match action:
        case RemoveWorktree(path=path):
            git.run_git("worktree", "remove", str(path), cwd=repo)
        case DeleteBranch(name=name):
            git.run_git("branch", "-D", name, cwd=repo)


def _recovery(candidate: Candidate, done: Sequence[Action]) -> tuple[str, ...]:
    commands = []
    for action in done:
        match action:
            case DeleteBranch(name=name, tip=tip):
                commands.append(f"git branch {name} {tip}")
            case RemoveWorktree(path=path) if candidate.worktree and candidate.worktree.detached:
                commands.append(f"git worktree add --detach {path} {candidate.worktree.head}")
    return tuple(commands)
