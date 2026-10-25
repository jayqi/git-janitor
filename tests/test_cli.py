from datetime import UTC, datetime, timedelta
import json
import os
import shutil

import pytest
from typer.testing import CliRunner

from git_janitor import cli, git

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
    pop_last_activity(output["candidates"][0])
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


def report_json(*targets):
    result = runner.invoke(cli.app, ["report", "--json", "--no-fetch", *targets])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def pop_last_activity(candidate):
    """Remove `last_activity` from a candidate's JSON, checking it's a time from this test."""
    last_activity = datetime.fromisoformat(candidate.pop("last_activity"))
    assert datetime.now(UTC) - timedelta(minutes=5) < last_activity <= datetime.now(UTC)


def test_report_json_extra_commits(in_sandbox):
    path, pr = merged_branch_in_worktree(in_sandbox)
    extra = in_sandbox.commit(cwd=path, message="Follow-up")

    [candidate] = report_json("feat")["candidates"]

    assert candidate["status"] == "merged_plus_local"
    assert candidate["evidence"][-1] == {
        "kind": "extra_commits",
        "pull_request": pr,
        "commits": [{"sha": extra, "subject": "Follow-up"}],
    }


def test_report_json_detached_worktree(in_sandbox):
    path = in_sandbox.add_detached_worktree("scratch")
    head = in_sandbox.tip("main")

    [candidate] = report_json(str(path))["candidates"]

    pop_last_activity(candidate)
    assert candidate == {
        "id": str(path),
        "verdict": "review",
        "status": "no_unique_commits",
        "branch": None,
        "worktree": {
            "path": str(path),
            "head": head,
            "detached": True,
            "is_main": False,
            "locked": False,
            "prunable": False,
        },
        "blockers": [
            {
                "code": "recent_activity",
                "detail": "active within the last 7d (janitor.minAge)",
                "min_age": "7d",
            }
        ],
        "evidence": [],
        "actions": [{"kind": "remove_worktree", "path": str(path)}],
    }


def test_report_json_worktree_blockers(in_sandbox, global_excludes):
    global_excludes.write_text(".plans/\n")
    path, _ = merged_branch_in_worktree(in_sandbox)
    in_sandbox.new_branch("other")
    in_sandbox.git("merge", "--no-commit", "--no-ff", "other", cwd=path)
    in_sandbox.git("worktree", "lock", str(path))
    (path / "notes.txt").write_text("notes\n")
    (path / ".plans").mkdir()
    plan = path / ".plans" / "plan.md"
    plan.write_text("plan\n")
    modified = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    os.utime(plan, (modified.timestamp(), modified.timestamp()))

    [candidate] = report_json("feat")["candidates"]

    assert candidate["blockers"] == [
        {"code": "locked", "detail": "the worktree is locked"},
        {"code": "in_progress_op", "detail": "merge in progress", "operation": "merge"},
        {"code": "dirty", "detail": "1 modified or staged files", "paths": ["file3.txt"]},
        {"code": "untracked", "detail": "1 untracked files", "paths": ["notes.txt"]},
        {
            "code": "precious_files",
            "detail": "1 precious ignored files",
            "files": [
                {"path": ".plans/plan.md", "size": 5, "modified": "2026-01-02T03:04:05+00:00"}
            ],
        },
    ]


def test_report_json_main_worktree_and_stash_blockers(in_sandbox):
    in_sandbox.new_branch("feat")
    in_sandbox.squash_merge(in_sandbox.open_pr("feat"))
    in_sandbox.git("checkout", "-q", "feat")
    (in_sandbox.repo / "file1.txt").write_text("unsaved\n")
    in_sandbox.git("stash", "push", "-m", "idea")

    [candidate] = report_json("feat")["candidates"]

    assert candidate["blockers"] == [
        {"code": "is_main", "detail": "checked out in the main worktree"},
        {
            "code": "stashes",
            "detail": "1 stash entries made on this branch",
            "stashes": [{"ref": "stash@{0}", "subject": "On feat: idea"}],
        },
    ]


def test_report_json_current_worktree_blocker(in_sandbox, monkeypatch):
    path, _ = merged_branch_in_worktree(in_sandbox)
    monkeypatch.chdir(path)

    [candidate] = report_json("feat")["candidates"]

    assert candidate["blockers"] == [
        {"code": "is_current", "detail": "the current directory is in this worktree"}
    ]


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


def clean_json(*args):
    result = runner.invoke(cli.app, ["clean", "--json", "--no-fetch", *args])
    return result.exit_code, json.loads(result.stdout)


def test_clean_json_would_delete(in_sandbox):
    path, _ = merged_branch_in_worktree(in_sandbox)
    tip = in_sandbox.tip("feat")

    exit_code, output = clean_json("--dry-run", "feat")

    assert exit_code == 0
    assert output == {
        "schema_version": 1,
        "dry_run": True,
        "outcomes": [
            {
                "id": "feat",
                "outcome": "would_delete",
                "verdict": "delete",
                "detail": "",
                "actions": [
                    {"kind": "remove_worktree", "path": str(path)},
                    {"kind": "delete_branch", "name": "feat", "tip": tip},
                ],
                "recovery": [f"git branch feat {tip}"],
                "accepted": [],
            }
        ],
    }


def test_clean_json_refused_and_not_found(in_sandbox):
    tip = in_sandbox.new_branch("feat")
    in_sandbox.open_pr("feat")

    exit_code, output = clean_json("feat", "nope")

    assert exit_code == 1
    assert output["outcomes"] == [
        {
            "id": "feat",
            "outcome": "refused",
            "verdict": "keep",
            "detail": "verdict is keep, not delete",
            "actions": [{"kind": "delete_branch", "name": "feat", "tip": tip}],
            "recovery": [],
            "accepted": [],
        },
        {
            "id": "nope",
            "outcome": "not_found",
            "verdict": None,
            "detail": "no such candidate",
            "actions": [],
            "recovery": [],
            "accepted": [],
        },
    ]


def test_clean_json_failed(in_sandbox, unremovable_worktree):
    tip = in_sandbox.tip("feat")

    exit_code, output = clean_json("feat")

    assert exit_code == 1
    [outcome] = output["outcomes"]
    assert outcome.pop("detail").startswith("git worktree remove")
    assert outcome == {
        "id": "feat",
        "outcome": "failed",
        "verdict": "delete",
        "actions": [
            {"kind": "remove_worktree", "path": str(unremovable_worktree)},
            {"kind": "delete_branch", "name": "feat", "tip": tip},
        ],
        "recovery": [],
        "accepted": [],
    }


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


def test_missing_git_fails_cleanly(in_sandbox, monkeypatch):
    monkeypatch.setenv("PATH", "")

    result = runner.invoke(cli.app, ["report"])

    assert result.exit_code == 1
    assert "git is not installed" in result.stderr


def test_clean_with_targets_and_all_is_an_error(in_sandbox):
    result = runner.invoke(cli.app, ["clean", "--all", "feat"])

    assert result.exit_code != 0
    assert "--all" in result.stderr


def test_report_json_branch_without_worktree_and_unmatched_pull_request(in_sandbox):
    in_sandbox.new_branch("feat")
    old_pr = in_sandbox.open_pr("feat")
    in_sandbox.squash_merge(old_pr)
    old_head = in_sandbox.tip("feat")
    in_sandbox.git("branch", "-D", "feat")
    in_sandbox.git("fetch", "-q", "origin")
    tip = in_sandbox.new_branch("feat", start="origin/main")

    [candidate] = report_json("feat")["candidates"]

    pop_last_activity(candidate)
    assert candidate == {
        "id": "feat",
        "verdict": "review",
        "status": "no_pr",
        "branch": {"name": "feat", "tip": tip, "upstream": "origin/main", "upstream_gone": False},
        "worktree": None,
        "blockers": [],
        "evidence": [
            {
                "kind": "pull_request",
                "number": old_pr,
                "url": f"https://github.com/owner/repo/pull/{old_pr}",
                "state": "MERGED",
                "base": "main",
                "head_oid": old_head,
                "matched": False,
            }
        ],
        "actions": [{"kind": "delete_branch", "name": "feat", "tip": tip}],
    }


def test_report_json_missing_worktree(in_sandbox):
    path, _ = merged_branch_in_worktree(in_sandbox)
    shutil.rmtree(path)

    [candidate] = report_json("feat")["candidates"]

    assert candidate["verdict"] == "delete"
    assert candidate["worktree"] == {
        "path": str(path),
        "head": in_sandbox.tip("feat"),
        "detached": False,
        "is_main": False,
        "locked": False,
        "prunable": True,
    }
    assert candidate["blockers"] == []


def test_clean_json_deleted(in_sandbox):
    path, _ = merged_branch_in_worktree(in_sandbox)
    tip = in_sandbox.tip("feat")

    exit_code, output = clean_json("feat")

    assert exit_code == 0
    assert output["outcomes"] == [
        {
            "id": "feat",
            "outcome": "deleted",
            "verdict": "delete",
            "detail": "",
            "actions": [
                {"kind": "remove_worktree", "path": str(path)},
                {"kind": "delete_branch", "name": "feat", "tip": tip},
            ],
            "recovery": [f"git branch feat {tip}"],
            "accepted": [],
        }
    ]


def test_report_json_error_blocker(in_sandbox, monkeypatch):
    merged_branch_in_worktree(in_sandbox)

    def fail(cwd):
        raise PermissionError(13, "Permission denied", str(cwd))

    monkeypatch.setattr(git, "worktree_status", fail)

    [candidate] = report_json("feat")["candidates"]

    assert candidate["verdict"] == "review"
    assert candidate["blockers"] == [
        {
            "code": "error",
            "detail": f"[Errno 13] Permission denied: '{candidate['worktree']['path']}'",
        }
    ]


def test_report_table_shows_paths_with_markup_characters(in_sandbox, monkeypatch):
    monkeypatch.setenv("COLUMNS", "500")
    in_sandbox.add_detached_worktree("[bold]")

    result = runner.invoke(cli.app, ["report", "--no-fetch"])

    assert result.exit_code == 0, result.output
    assert "wt-[bold]" in result.stdout


def test_clean_output_shows_targets_with_markup_characters(in_sandbox):
    result = runner.invoke(cli.app, ["clean", "--no-fetch", "[/x]"])

    assert result.exit_code == 1
    assert "[/x]: not_found" in result.stdout


def test_report_lists_each_candidate_once(in_sandbox):
    path, _ = merged_branch_in_worktree(in_sandbox)

    output = report_json("feat", str(path))

    assert [c["id"] for c in output["candidates"]] == ["feat"]


def test_report_json_repo_is_main_worktree(in_sandbox, monkeypatch):
    path, _ = merged_branch_in_worktree(in_sandbox)
    monkeypatch.chdir(path)

    assert report_json()["repo"] == str(in_sandbox.repo)


def test_report_json_nested_repository_and_unreadable_blockers(
    in_sandbox, global_excludes, restore_permissions
):
    global_excludes.write_text(".plans/\n")
    path, _ = merged_branch_in_worktree(in_sandbox)
    in_sandbox.git("init", "-q", str(path / ".plans" / "experiment"))
    private = path / ".plans" / "private"
    private.mkdir()
    private.chmod(0o000)
    restore_permissions.append(private)

    [candidate] = report_json("feat")["candidates"]

    assert candidate["blockers"] == [
        {
            "code": "nested_repository",
            "detail": "1 git repositories inside ignored directories",
            "paths": [".plans/experiment"],
        },
        {
            "code": "unreadable",
            "detail": "1 ignored paths couldn't be read",
            "paths": [".plans/private"],
        },
    ]


def test_report_json_branch_missing_worktree(in_sandbox):
    path = in_sandbox.root / "wt-orphan"
    in_sandbox.git("worktree", "add", "-q", "--orphan", "-b", "newthing", str(path))

    [candidate] = report_json(str(path))["candidates"]

    pop_last_activity(candidate)
    assert candidate == {
        "id": str(path),
        "verdict": "review",
        "status": "branch_missing",
        "branch": None,
        "worktree": {
            "path": str(path),
            "head": "0" * 40,
            "detached": False,
            "is_main": False,
            "locked": False,
            "prunable": False,
        },
        "blockers": [],
        "evidence": [],
        "actions": [{"kind": "remove_worktree", "path": str(path)}],
    }


def test_clean_json_accepted_blockers(in_sandbox, global_excludes):
    global_excludes.write_text(".plans/\n")
    path, _ = merged_branch_in_worktree(in_sandbox)
    (path / ".plans").mkdir()
    (path / ".plans" / "plan.md").write_text("plan\n")

    plan = path / ".plans" / "plan.md"
    modified = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    os.utime(plan, (modified.timestamp(), modified.timestamp()))
    tip = in_sandbox.tip("feat")

    exit_code, output = clean_json("--accept", "precious_files, stashes", "feat")

    assert exit_code == 0
    assert output["outcomes"] == [
        {
            "id": "feat",
            "outcome": "deleted",
            "verdict": "review",
            "detail": "",
            "actions": [
                {"kind": "remove_worktree", "path": str(path)},
                {"kind": "delete_branch", "name": "feat", "tip": tip},
            ],
            "recovery": [f"git branch feat {tip}"],
            "accepted": [
                {
                    "code": "precious_files",
                    "detail": "1 precious ignored files",
                    "files": [
                        {
                            "path": ".plans/plan.md",
                            "size": 5,
                            "modified": "2026-01-02T03:04:05+00:00",
                        }
                    ],
                }
            ],
        }
    ]


def test_clean_json_refused_with_blockers(in_sandbox):
    path, _ = merged_branch_in_worktree(in_sandbox)
    (path / "notes.txt").write_text("notes\n")
    tip = in_sandbox.tip("feat")

    exit_code, output = clean_json("feat")

    assert exit_code == 1
    assert output["outcomes"] == [
        {
            "id": "feat",
            "outcome": "refused",
            "verdict": "review",
            "detail": "verdict is review, not delete; blockers: untracked",
            "actions": [
                {"kind": "remove_worktree", "path": str(path)},
                {"kind": "delete_branch", "name": "feat", "tip": tip},
            ],
            "recovery": [],
            "accepted": [],
        }
    ]


def test_clean_accept_rejects_other_blockers(in_sandbox):
    result = runner.invoke(cli.app, ["clean", "--accept", "untracked", "feat"])

    assert result.exit_code == 1
    assert "untracked" in result.stderr


def test_clean_accept_rejects_unknown_blockers(in_sandbox):
    result = runner.invoke(cli.app, ["clean", "--accept", "nonsense", "feat"])

    assert result.exit_code == 1
    assert "nonsense" in result.stderr


def test_report_json_without_recorded_activity(in_sandbox):
    in_sandbox.git("-c", "core.logAllRefUpdates=false", "branch", "scratch", "main")

    [candidate] = report_json("scratch")["candidates"]

    assert candidate["last_activity"] is None
