from collections.abc import Collection, Sequence
from datetime import datetime
from pathlib import Path
import shlex

from git_janitor import git
from git_janitor.analysis import analyze, worktree_blockers
from git_janitor.github import GitHubClient
from git_janitor.model import (
    ACCEPTABLE_BLOCKERS,
    Action,
    BlockerCode,
    Candidate,
    CleanResult,
    DeleteBranch,
    Outcome,
    RemoveWorktree,
    TargetOutcome,
    Verdict,
)


def clean(
    repo: Path,
    github: GitHubClient,
    *,
    targets: Sequence[str] = (),
    all: bool = False,
    dry_run: bool = False,
    fetch: bool = True,
    cwd: Path | None = None,
    now: datetime | None = None,
    accept: Collection[BlockerCode] = (),
) -> CleanResult:
    """Re-analyze `repo` and carry out the actions of targets with a delete verdict.

    Each action first checks that its target hasn't changed since the analysis. Blockers with
    codes in `accept` are ignored for explicit targets.
    """
    if bool(targets) == all:
        raise ValueError("Pass candidate ids to clean, or --all, but not both.")
    accept = frozenset(accept)
    if not_acceptable := accept - ACCEPTABLE_BLOCKERS:
        codes = ", ".join(sorted(not_acceptable))
        raise ValueError(f"{codes} can't be accepted")
    if accept and all:
        raise ValueError("Accepting blockers requires explicit targets.")
    cwd = (cwd or Path.cwd()).resolve()
    report = analyze(repo, github, fetch=fetch, cwd=cwd, now=now)

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
        else _clean_candidate(item, repo, cwd, accept, dry_run=dry_run)
        for item in selected
    ]
    return CleanResult(report=report, outcomes=tuple(outcomes))


class ChangedSinceAnalysisError(Exception):
    pass


def _clean_candidate(
    candidate: Candidate,
    repo: Path,
    cwd: Path,
    accept: frozenset[BlockerCode],
    *,
    dry_run: bool,
) -> TargetOutcome:
    accepted = tuple(b for b in candidate.blockers if b.code in accept)

    def outcome(outcome: Outcome, detail: str = "", done: Sequence[Action] = ()) -> TargetOutcome:
        return TargetOutcome(
            id=candidate.id,
            outcome=outcome,
            verdict=candidate.verdict,
            detail=detail,
            actions=candidate.actions,
            recovery=_recovery(done),
            accepted=accepted if outcome is not Outcome.REFUSED else (),
        )

    verdict = candidate.verdict_accepting(accept)
    if verdict is not Verdict.DELETE:
        detail = f"verdict is {verdict}, not delete"
        if blockers := candidate.unaccepted_blockers(accept):
            detail += f"; blockers: {', '.join(str(b.code) for b in blockers)}"
        return outcome(Outcome.REFUSED, detail)
    if dry_run:
        return outcome(Outcome.WOULD_DELETE, done=candidate.actions)

    done: list[Action] = []
    for action in candidate.actions:
        try:
            _execute(action, repo, cwd, accept)
        except Exception as exc:  # Keep going, so earlier targets' recovery commands are reported
            return outcome(Outcome.FAILED, f"{type(exc).__name__}: {exc}", done)
        done.append(action)
    return outcome(Outcome.DELETED, done=done)


def _execute(action: Action, repo: Path, cwd: Path, accept: frozenset[BlockerCode]) -> None:
    worktrees = git.list_worktrees(repo)
    match action:
        case RemoveWorktree(path=path):
            current = next((wt for wt in worktrees if wt.path == path), None)
            if current is None:
                raise ChangedSinceAnalysisError(f"{path} is no longer a worktree")
            blockers = worktree_blockers(current, cwd)
            if unaccepted := [b for b in blockers if b.code not in accept]:
                found = "; ".join(f"{b.code} ({b.detail})" for b in unaccepted)
                raise ChangedSinceAnalysisError(f"{path} has new blockers: {found}")
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
