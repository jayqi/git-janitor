from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from git_janitor import git
from git_janitor.analysis import (
    Action,
    Candidate,
    DeleteBranch,
    RemoveWorktree,
    Report,
    Verdict,
    analyze,
)
from git_janitor.github import GitHubClient


class Outcome(StrEnum):
    DELETED = "deleted"
    WOULD_DELETE = "would_delete"
    REFUSED = "refused"
    NOT_FOUND = "not_found"
    FAILED = "failed"


@dataclass
class TargetOutcome:
    id: str
    outcome: Outcome
    verdict: Verdict | None = None
    detail: str = ""
    actions: list[Action] = field(default_factory=list)
    recovery: list[str] = field(default_factory=list)


@dataclass
class CleanResult:
    report: Report
    outcomes: list[TargetOutcome]

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

    outcomes = []
    if all:
        selected = [c for c in report.candidates if c.verdict is Verdict.DELETE]
    else:
        selected = []
        for target in targets:
            candidate = report.find(target, cwd=cwd)
            if candidate is None:
                outcomes.append(
                    TargetOutcome(id=target, outcome=Outcome.NOT_FOUND, detail="no such candidate")
                )
            else:
                selected.append(candidate)

    for candidate in selected:
        outcomes.append(_clean_candidate(candidate, repo, dry_run=dry_run))
    return CleanResult(report=report, outcomes=outcomes)


def _clean_candidate(candidate: Candidate, repo: Path, *, dry_run: bool) -> TargetOutcome:
    outcome = TargetOutcome(
        id=candidate.id,
        outcome=Outcome.WOULD_DELETE if dry_run else Outcome.DELETED,
        verdict=candidate.verdict,
        actions=candidate.actions,
        recovery=_recovery(candidate),
    )
    if candidate.verdict is not Verdict.DELETE:
        outcome.outcome = Outcome.REFUSED
        outcome.detail = f"verdict is {candidate.verdict}, not delete"
        outcome.recovery = []
        return outcome
    if dry_run:
        return outcome

    for action in candidate.actions:
        try:
            match action:
                case RemoveWorktree(path=path):
                    git.run_git("worktree", "remove", str(path), cwd=repo)
                case DeleteBranch(name=name):
                    git.run_git("branch", "-D", name, cwd=repo)
        except git.GitError as exc:
            outcome.outcome = Outcome.FAILED
            outcome.detail = str(exc)
            break
    return outcome


def _recovery(candidate: Candidate) -> list[str]:
    """Commands that restore the commits a candidate's actions make unreachable."""
    commands = []
    for action in candidate.actions:
        match action:
            case DeleteBranch(name=name, tip=tip):
                commands.append(f"git branch {name} {tip}")
            case RemoveWorktree(path=path) if candidate.branch is None:
                assert candidate.worktree is not None
                commands.append(f"git worktree add --detach {path} {candidate.worktree.head}")
    return commands
