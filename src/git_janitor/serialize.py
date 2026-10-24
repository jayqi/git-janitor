"""JSON-compatible representations of reports and clean results. See README.md for the schema."""

from __future__ import annotations

from typing import Any

from git_janitor.analysis import (
    Action,
    Blocker,
    Candidate,
    DeleteBranch,
    Evidence,
    ExtraCommits,
    PullRequestEvidence,
    RemoveWorktree,
    Report,
)
from git_janitor.clean import CleanResult, TargetOutcome

SCHEMA_VERSION = 1


def report_to_dict(report: Report, candidates: list[Candidate] | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "repo": str(report.repo),
        "github_repo": report.github_repo,
        "default_branch": report.default_branch,
        "fetched": report.fetched,
        "candidates": [
            candidate_to_dict(c) for c in (report.candidates if candidates is None else candidates)
        ],
    }


def clean_result_to_dict(result: CleanResult, *, dry_run: bool) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "dry_run": dry_run,
        "outcomes": [outcome_to_dict(o) for o in result.outcomes],
    }


def candidate_to_dict(candidate: Candidate) -> dict[str, Any]:
    branch = candidate.branch
    worktree = candidate.worktree
    return {
        "id": candidate.id,
        "verdict": str(candidate.verdict),
        "status": str(candidate.status),
        "branch": branch
        and {
            "name": branch.name,
            "tip": branch.tip,
            "upstream": branch.upstream,
            "upstream_gone": branch.upstream_gone,
        },
        "worktree": worktree
        and {
            "path": str(worktree.path),
            "head": worktree.head,
            "detached": worktree.branch is None,
            "is_main": worktree.is_main,
            "locked": worktree.locked,
            "prunable": worktree.prunable,
        },
        "blockers": [blocker_to_dict(b) for b in candidate.blockers],
        "evidence": [evidence_to_dict(e) for e in candidate.evidence],
        "actions": [action_to_dict(a) for a in candidate.actions],
    }


def blocker_to_dict(blocker: Blocker) -> dict[str, Any]:
    result: dict[str, Any] = {"code": str(blocker.code), "detail": blocker.detail}
    if blocker.paths:
        result["paths"] = list(blocker.paths)
    if blocker.files:
        result["files"] = [
            {"path": f.path, "size": f.size, "modified": f.modified.isoformat()}
            for f in blocker.files
        ]
    if blocker.stashes:
        result["stashes"] = [{"ref": s.ref, "subject": s.subject} for s in blocker.stashes]
    return result


def evidence_to_dict(evidence: Evidence) -> dict[str, Any]:
    match evidence:
        case PullRequestEvidence(pull_request=pr, matched=matched):
            return {
                "kind": "pull_request",
                "number": pr.number,
                "url": pr.url,
                "state": str(pr.state),
                "base": pr.base,
                "head_oid": pr.head_oid,
                "matched": matched,
            }
        case ExtraCommits(pull_request=number, commits=commits):
            return {
                "kind": "extra_commits",
                "pull_request": number,
                "commits": [{"sha": c.sha, "subject": c.subject} for c in commits],
            }


def action_to_dict(action: Action) -> dict[str, Any]:
    match action:
        case RemoveWorktree(path=path):
            return {"kind": "remove_worktree", "path": str(path)}
        case DeleteBranch(name=name, tip=tip):
            return {"kind": "delete_branch", "name": name, "tip": tip}


def outcome_to_dict(outcome: TargetOutcome) -> dict[str, Any]:
    return {
        "id": outcome.id,
        "outcome": str(outcome.outcome),
        "verdict": outcome.verdict and str(outcome.verdict),
        "detail": outcome.detail,
        "actions": [action_to_dict(a) for a in outcome.actions],
        "recovery": outcome.recovery,
    }
