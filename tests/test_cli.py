import json

import pytest
from typer.testing import CliRunner

from git_janitor import cli

runner = CliRunner()


@pytest.fixture
def in_sandbox(sandbox, monkeypatch):
    monkeypatch.chdir(sandbox.repo)
    monkeypatch.setattr(cli, "github_client", lambda: sandbox.github)
    return sandbox


def merged_branch_in_worktree(sandbox, name="feat"):
    path = sandbox.add_worktree(name, create=True)
    sandbox.commit(cwd=path)
    pr = sandbox.open_pr(name)
    sandbox.squash_merge(pr)
    return path, pr


def test_report_json(in_sandbox):
    path, pr = merged_branch_in_worktree(in_sandbox)
    tip = in_sandbox.tip("feat")

    result = runner.invoke(cli.app, ["report", "--json", "feat"])

    assert result.exit_code == 0, result.output
    output = json.loads(result.stdout)
    assert output["schema_version"] == 1
    assert output["repo"] == str(in_sandbox.repo)
    assert output["github_repo"] == "owner/repo"
    assert output["default_branch"] == "main"
    assert output["fetched"] is True
    assert output["candidates"] == [
        {
            "id": "feat",
            "verdict": "delete",
            "status": "merged",
            "branch": {
                "name": "feat",
                "tip": tip,
                "upstream": "origin/feat",
                "upstream_gone": True,
            },
            "worktree": {
                "path": str(path),
                "head": tip,
                "detached": False,
                "is_main": False,
                "locked": False,
                "prunable": False,
            },
            "blockers": [],
            "evidence": [
                {
                    "kind": "pull_request",
                    "number": pr,
                    "url": f"https://github.com/owner/repo/pull/{pr}",
                    "state": "MERGED",
                    "base": "main",
                    "head_oid": tip,
                    "matched": True,
                }
            ],
            "actions": [
                {"kind": "remove_worktree", "path": str(path)},
                {"kind": "delete_branch", "name": "feat", "tip": tip},
            ],
        }
    ]


def test_report_json_blocker_and_extra_commit_details(in_sandbox, global_excludes):
    global_excludes.write_text(".plans/\n")
    path, pr = merged_branch_in_worktree(in_sandbox)
    [extra] = [in_sandbox.commit(cwd=path, message="Follow-up")]
    (path / ".plans").mkdir()
    (path / ".plans" / "plan.md").write_text("plan\n")

    result = runner.invoke(cli.app, ["report", "--json", "feat"])

    [candidate] = json.loads(result.stdout)["candidates"]
    assert candidate["status"] == "merged_plus_local"
    assert candidate["evidence"][-1] == {
        "kind": "extra_commits",
        "pull_request": pr,
        "commits": [{"sha": extra, "subject": "Follow-up"}],
    }
    [blocker] = candidate["blockers"]
    assert blocker["code"] == "precious_files"
    [file] = blocker["files"]
    assert file["path"] == ".plans/plan.md"
    assert file["size"] == 5
    assert "modified" in file


def test_report_table_shows_candidates(in_sandbox):
    merged_branch_in_worktree(in_sandbox)

    result = runner.invoke(cli.app, ["report", "--no-fetch"])

    assert result.exit_code == 0, result.output
    assert "feat" in result.stdout
    assert "delete" in result.stdout


def test_report_unknown_target_fails(in_sandbox):
    result = runner.invoke(cli.app, ["report", "nope"])

    assert result.exit_code == 1
    assert "nope" in result.stderr


def test_clean_deletes_and_prints_recovery(in_sandbox):
    merged_branch_in_worktree(in_sandbox)
    tip = in_sandbox.tip("feat")

    result = runner.invoke(cli.app, ["clean", "feat"])

    assert result.exit_code == 0, result.output
    assert f"git branch feat {tip}" in result.stdout


def test_clean_json(in_sandbox):
    merged_branch_in_worktree(in_sandbox)
    tip = in_sandbox.tip("feat")

    result = runner.invoke(cli.app, ["clean", "--json", "--dry-run", "feat"])

    output = json.loads(result.stdout)
    assert output["schema_version"] == 1
    assert output["dry_run"] is True
    [outcome] = output["outcomes"]
    assert outcome["id"] == "feat"
    assert outcome["outcome"] == "would_delete"
    assert outcome["verdict"] == "delete"
    assert outcome["recovery"] == [f"git branch feat {tip}"]


def test_clean_refusal_exits_nonzero(in_sandbox):
    in_sandbox.new_branch("feat")
    in_sandbox.open_pr("feat")

    result = runner.invoke(cli.app, ["clean", "feat"])

    assert result.exit_code == 1
    assert "refused" in result.stdout


def test_clean_without_targets_is_an_error(in_sandbox):
    result = runner.invoke(cli.app, ["clean"])

    assert result.exit_code != 0
    assert "--all" in result.stderr


def test_outside_a_git_repository_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli.app, ["report"])

    assert result.exit_code == 1
    assert "Error" in result.stderr


def test_no_subcommand_prints_help():
    result = runner.invoke(cli.app, [])

    assert "report" in result.output
    assert "clean" in result.output
