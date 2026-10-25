from collections.abc import Sequence
import json
import os
from pathlib import Path
from typing import Annotated

from rich.console import Console
from rich.table import Table
import typer

from git_janitor import git
from git_janitor.analysis import analyze
from git_janitor.clean import CleanResult, clean
from git_janitor.github import GhGitHubClient, GitHubClient, GitHubError
from git_janitor.model import Candidate, PullRequestEvidence, Verdict
from git_janitor.serialize import clean_result_to_dict, report_to_dict

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Find local branches and worktrees whose work is merged on GitHub, and clean them up.",
)
console = Console()
err_console = Console(stderr=True)

VERDICT_STYLES = {Verdict.DELETE: "green", Verdict.KEEP: "blue", Verdict.REVIEW: "yellow"}

Targets = Annotated[
    list[str] | None, typer.Argument(help="Candidate ids: branch names or worktree paths.")
]
JsonOption = Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")]
NoFetchOption = Annotated[
    bool, typer.Option("--no-fetch", help="Skip `git fetch origin --prune`.")
]


def github_client() -> GitHubClient:
    return GhGitHubClient()


def fail(message: str) -> typer.Exit:
    err_console.print(f"[red]Error:[/red] {message}", highlight=False)
    return typer.Exit(code=1)


def repo_root() -> Path:
    try:
        return Path(git.run_git("rev-parse", "--show-toplevel", cwd=Path.cwd()).strip())
    except git.GitError as exc:
        raise fail(str(exc)) from exc


@app.command()
def report(
    targets: Targets = None,
    as_json: JsonOption = False,
    no_fetch: NoFetchOption = False,
) -> None:
    """Report the merge status and verdict of local branches and worktrees."""
    repo = repo_root()
    try:
        result = analyze(repo, github_client(), fetch=not no_fetch)
    except (git.GitError, GitHubError) as exc:
        raise fail(str(exc)) from exc

    candidates = result.candidates
    if targets:
        found = {target: result.find(target, cwd=Path.cwd()) for target in targets}
        missing = [target for target, candidate in found.items() if candidate is None]
        if missing:
            raise fail(f"No such candidates: {', '.join(missing)}")
        candidates = [c for c in found.values() if c is not None]

    if as_json:
        print(json.dumps(report_to_dict(result, candidates), indent=2))
    else:
        console.print(candidates_table(candidates))


@app.command(name="clean")
def clean_command(
    targets: Targets = None,
    all: Annotated[
        bool, typer.Option("--all", help="Clean every candidate whose verdict is delete.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be deleted.")
    ] = False,
    as_json: JsonOption = False,
    no_fetch: NoFetchOption = False,
) -> None:
    """Delete candidates whose verdict is delete. Anything else is refused."""
    if not targets and not all:
        raise fail("Pass candidate ids to clean, or --all.")
    repo = repo_root()
    try:
        result = clean(
            repo,
            github_client(),
            targets=targets or [],
            all=all,
            dry_run=dry_run,
            fetch=not no_fetch,
        )
    except (git.GitError, GitHubError) as exc:
        raise fail(str(exc)) from exc

    if as_json:
        print(json.dumps(clean_result_to_dict(result, dry_run=dry_run), indent=2))
    else:
        print_clean_result(result)
    if not result.ok:
        raise typer.Exit(code=1)


def candidates_table(candidates: Sequence[Candidate]) -> Table:
    table = Table()
    for column in ("Candidate", "Verdict", "Status", "Worktree", "Blockers", "Pull request"):
        table.add_column(column, overflow="fold")
    for candidate in candidates:
        style = VERDICT_STYLES[candidate.verdict]
        matched = [
            e.pull_request
            for e in candidate.evidence
            if isinstance(e, PullRequestEvidence) and e.matched
        ]
        table.add_row(
            candidate.id,
            f"[{style}]{candidate.verdict}[/{style}]",
            str(candidate.status),
            os.path.relpath(candidate.worktree.path) if candidate.worktree else "",
            ", ".join(str(b.code) for b in candidate.blockers),
            f"#{matched[0].number}" if matched else "",
        )
    return table


def print_clean_result(result: CleanResult) -> None:
    for outcome in result.outcomes:
        line = f"{outcome.id}: {outcome.outcome}"
        if outcome.detail:
            line += f" ({outcome.detail})"
        console.print(line, highlight=False)
        for command in outcome.recovery:
            console.print(f"  to recover: {command}", highlight=False)
    if not result.outcomes:
        console.print("Nothing to clean.")
