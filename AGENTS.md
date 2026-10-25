# AGENTS instructions

git-janitor is a Python CLI that gathers evidence on whether local git branches and worktrees are merged into origin's default branch on GitHub, including squash and rebase merges, and deletes the ones that verifiably are. [README.md](/README.md) documents the user-facing behavior: commands, statuses, verdicts, blockers, and JSON output. Treat it as the spec.

## Development environment

This project uses Just. Use the following `justfile` commands for code quality checks:

- **Test suite**: `just test [--python VERSION] -- [ARGS]`
  - Use `--python` to run for specific Python version
  - Positional arguments after `--` are passed to pytest. Use to pass pytest flags like `-vv` or to run on specific paths.
- **Linting**: `just lint [ARGS]`
  - Positional arguments are passed to Ruff. Use to run on specific paths.
- **Formatting**: `just format [ARGS]`
  - Positional arguments are passed to Ruff. Use to run on specific paths.
- **Static typechecking**: `just typecheck [--python VERSION] -- [ARGS]`
  - Use `--python` to run for specific Python version
  - Positional arguments after `--` are passed to the typechecker. Use to run on specific paths.

This project uses uv for managing Python environments. Use `uv run` instead of `python` to run Python code.

Use red/green test-driven development: write failing tests first before implementing changes that make them pass. Tests use pytest and go in [`tests/`](/tests/).

## Architecture

The analysis runs in this order:

1. Fetch origin (unless `--no-fetch`).
2. Query GitHub once, in batches, for the default branch and the pull requests for every local branch's head name.
3. Classify each **tip** (a branch ref or a detached worktree HEAD) into a status.
4. Check each worktree for blockers.
5. Join them into **candidates**: one per branch, branch plus worktree, or detached worktree. Each gets a verdict and an ordered list of actions.

`clean` re-runs the full analysis and executes the actions of the requested candidates whose verdict is `delete`.

Modules:

- `model`: the domain model, as frozen dataclasses. Facts gathered from git and GitHub (branches, worktrees, pull requests) and the conclusions drawn from them (statuses, blockers, evidence, actions, candidates). Variants are separate types joined in unions (`Blocker`, `Evidence`, `Action`), and derived values like `Candidate.verdict` are computed properties, not stored fields.
- `git`, `github`, `precious`: adapters that gather facts and return `model` types. Git is accessed with `subprocess` and parsers for machine-readable output (`--porcelain`, `-z`, `--format`); don't add GitPython. GitHub is accessed through the `GitHubClient` protocol, whose real implementation shells out to `gh api graphql`.
- `analysis`, `clean`: the logic that turns facts into a `Report` and carries out deletions.
- `serialize`, `cli`: presentation. `serialize` is the only place the JSON shape is defined. A change to the JSON output must update the README; an incompatible change must also bump `SCHEMA_VERSION`.

## Safety invariants

These are the reasons the tool exists. Don't weaken them without an explicit decision from the maintainer.

- A pull request only counts if the local tip equals its head commit or is an ancestor of it. Matching by branch name alone is never enough.
- Only merges into the default branch count.
- Only `merged` candidates with no blockers get the `delete` verdict.
- `clean` re-runs the analysis at deletion time, refuses anything that isn't `delete`, and has no force option.
- Worktrees are removed with `git worktree remove` without `--force`, before their branch is deleted.
- Nothing on the remote is ever modified.

## Testing

- Git behavior is tested against real temporary repositories, with a bare repository standing in as `origin`. Don't mock git.
- The CLI tests compare JSON output against exact expected values, covering every variant of candidate, blocker, evidence, and clean outcome. Keep it that way when adding variants.
- GitHub is replaced with an in-memory fake implementing `GitHubClient`. To simulate a squash merge, a test creates the squash commit on the fake origin's default branch, creates `refs/pull/<N>/head` on the bare origin, and registers a merged pull request in the fake.
- There are no tests against live GitHub.
