import shutil

import pytest

from git_janitor import clean as clean_module
from git_janitor import git
from git_janitor.analysis import analyze
from git_janitor.clean import clean
from git_janitor.model import BlockerCode, Outcome, Verdict


def local_branches(sandbox):
    return sandbox.git("branch", "--format=%(refname:short)").splitlines()


def test_clean_removes_worktree_and_branch(sandbox):
    path, _ = sandbox.merged_worktree()
    tip = sandbox.tip("feat")

    result = clean(sandbox.repo, sandbox.github, targets=["feat"])

    [outcome] = result.outcomes
    assert outcome.id == "feat"
    assert outcome.outcome is Outcome.DELETED
    assert outcome.recovery == (f"git branch feat {tip}",)
    assert not path.exists()
    assert "feat" not in local_branches(sandbox)
    assert "branch.feat." not in sandbox.git("config", "--list")


def test_clean_removes_worktree_with_disposable_ignored_files(sandbox):
    path = sandbox.add_worktree("feat", create=True)
    (path / ".gitignore").write_text(".venv/\n")
    sandbox.git("add", ".gitignore", cwd=path)
    sandbox.git("commit", "-m", "Ignore venv", cwd=path)
    sandbox.squash_merge(sandbox.open_pr("feat"))
    (path / ".venv").mkdir()
    (path / ".venv" / "pyvenv.cfg").write_text("")

    result = clean(sandbox.repo, sandbox.github, targets=["feat"])

    assert result.outcomes[0].outcome is Outcome.DELETED
    assert not path.exists()


def test_clean_accepts_worktree_path_as_target(sandbox):
    path, _ = sandbox.merged_worktree()

    result = clean(sandbox.repo, sandbox.github, targets=[str(path)])

    assert result.outcomes[0].id == "feat"
    assert result.outcomes[0].outcome is Outcome.DELETED


def test_clean_refuses_target_without_delete_verdict(sandbox):
    sandbox.new_branch("feat")
    sandbox.open_pr("feat")

    result = clean(sandbox.repo, sandbox.github, targets=["feat"])

    [outcome] = result.outcomes
    assert outcome.outcome is Outcome.REFUSED
    assert outcome.verdict is Verdict.KEEP
    assert "feat" in local_branches(sandbox)


def test_clean_all_deletes_only_candidates_with_delete_verdict(sandbox):
    sandbox.new_branch("merged")
    sandbox.squash_merge(sandbox.open_pr("merged"))
    sandbox.new_branch("open")
    sandbox.open_pr("open")
    sandbox.new_branch("local-only")

    result = clean(sandbox.repo, sandbox.github, all=True)

    assert [(o.id, o.outcome) for o in result.outcomes] == [("merged", Outcome.DELETED)]
    assert sorted(local_branches(sandbox)) == ["local-only", "main", "open"]


def test_clean_dry_run_deletes_nothing(sandbox):
    path, _ = sandbox.merged_worktree()

    result = clean(sandbox.repo, sandbox.github, targets=["feat"], dry_run=True)

    assert result.outcomes[0].outcome is Outcome.WOULD_DELETE
    assert path.exists()
    assert "feat" in local_branches(sandbox)


def test_clean_keeps_branch_when_worktree_removal_fails(sandbox, unremovable_worktree):
    result = clean(sandbox.repo, sandbox.github, targets=["feat"])

    [outcome] = result.outcomes
    assert outcome.outcome is Outcome.FAILED
    assert "worktree remove" in outcome.detail
    assert outcome.recovery == ()
    assert "feat" in local_branches(sandbox)


@pytest.fixture
def after_analysis(monkeypatch):
    """Run a function between `clean`'s analysis and its deletions."""

    def install(race):
        def analyze_then_race(*args, **kwargs):
            report = analyze(*args, **kwargs)
            race()
            return report

        monkeypatch.setattr(clean_module, "analyze", analyze_then_race)

    return install


def test_clean_keeps_branch_that_moved(sandbox, after_analysis):
    sandbox.new_branch("feat")
    sandbox.squash_merge(sandbox.open_pr("feat"))
    moved = sandbox.git("commit-tree", "feat^{tree}", "-p", "feat", "-m", "Moved")
    after_analysis(lambda: sandbox.git("update-ref", "refs/heads/feat", moved))

    result = clean(sandbox.repo, sandbox.github, targets=["feat"])

    [outcome] = result.outcomes
    assert outcome.outcome is Outcome.FAILED
    assert outcome.recovery == ()
    assert sandbox.tip("feat") == moved


def test_clean_keeps_worktree_that_gained_precious_files(sandbox, global_excludes, after_analysis):
    global_excludes.write_text(".plans/\n")
    path, _ = sandbox.merged_worktree()

    def add_plan():
        (path / ".plans").mkdir()
        (path / ".plans" / "plan.md").write_text("plan\n")

    after_analysis(add_plan)

    result = clean(sandbox.repo, sandbox.github, targets=["feat"])

    [outcome] = result.outcomes
    assert outcome.outcome is Outcome.FAILED
    assert "precious_files" in outcome.detail
    assert (path / ".plans" / "plan.md").exists()


def test_clean_keeps_branch_checked_out_after_analysis(sandbox, after_analysis):
    sandbox.new_branch("feat")
    sandbox.squash_merge(sandbox.open_pr("feat"))
    after_analysis(lambda: sandbox.add_worktree("feat"))

    result = clean(sandbox.repo, sandbox.github, targets=["feat"])

    assert result.outcomes[0].outcome is Outcome.FAILED
    assert "feat" in local_branches(sandbox)


def test_clean_recovery_commands_are_shell_quoted(sandbox):
    sandbox.new_branch("feat;echo")
    sandbox.squash_merge(sandbox.open_pr("feat;echo"))
    tip = sandbox.tip("feat;echo")

    result = clean(sandbox.repo, sandbox.github, targets=["feat;echo"], dry_run=True)

    assert result.outcomes[0].recovery == (f"git branch 'feat;echo' {tip}",)


def test_clean_removes_branch_whose_worktree_directory_is_missing(sandbox):
    path, _ = sandbox.merged_worktree()
    shutil.rmtree(path)

    result = clean(sandbox.repo, sandbox.github, targets=["feat"])

    assert result.outcomes[0].outcome is Outcome.DELETED
    assert "feat" not in local_branches(sandbox)
    assert "wt-feat" not in sandbox.git("worktree", "list")


@pytest.mark.parametrize(
    "error", [PermissionError(13, "Permission denied"), RuntimeError("Permission denied")]
)
def test_clean_reports_errors_and_continues(sandbox, after_analysis, monkeypatch, error):
    first, _ = sandbox.merged_worktree("first")
    sandbox.merged_worktree("second")
    worktree_status = git.worktree_status

    def fail_for_first(cwd):
        if cwd == first:
            raise error
        return worktree_status(cwd)

    after_analysis(lambda: monkeypatch.setattr(git, "worktree_status", fail_for_first))

    result = clean(sandbox.repo, sandbox.github, all=True)

    assert [(o.id, o.outcome) for o in result.outcomes] == [
        ("first", Outcome.FAILED),
        ("second", Outcome.DELETED),
    ]
    assert "Permission denied" in result.outcomes[0].detail


def worktree_with_plans(sandbox, global_excludes):
    global_excludes.write_text(".plans/\n")
    path, _ = sandbox.merged_worktree()
    (path / ".plans").mkdir()
    (path / ".plans" / "plan.md").write_text("plan\n")
    return path


def test_clean_accepting_precious_files_deletes_worktree(sandbox, global_excludes):
    path = worktree_with_plans(sandbox, global_excludes)

    result = clean(
        sandbox.repo, sandbox.github, targets=["feat"], accept=[BlockerCode.PRECIOUS_FILES]
    )

    [outcome] = result.outcomes
    assert outcome.outcome is Outcome.DELETED
    assert outcome.verdict is Verdict.REVIEW
    [accepted] = outcome.accepted
    assert accepted.code is BlockerCode.PRECIOUS_FILES
    assert [e.path for e in accepted.entries] == [".plans/"]
    assert not path.exists()


def test_clean_refuses_blockers_that_were_not_accepted(sandbox, global_excludes):
    path = worktree_with_plans(sandbox, global_excludes)
    (path / "notes.txt").write_text("notes\n")

    result = clean(
        sandbox.repo, sandbox.github, targets=["feat"], accept=[BlockerCode.PRECIOUS_FILES]
    )

    [outcome] = result.outcomes
    assert outcome.outcome is Outcome.REFUSED
    assert outcome.detail == "verdict is review, not delete; blockers: untracked"
    assert outcome.accepted == ()
    assert path.exists()


def test_clean_accepting_recent_activity_deletes_fresh_worktree(sandbox):
    path = sandbox.add_worktree("fresh", create=True)

    result = clean(
        sandbox.repo, sandbox.github, targets=["fresh"], accept=[BlockerCode.RECENT_ACTIVITY]
    )

    assert result.outcomes[0].outcome is Outcome.DELETED
    assert not path.exists()


def test_clean_rejects_accept_with_all(sandbox):
    with pytest.raises(ValueError, match="targets"):
        clean(sandbox.repo, sandbox.github, all=True, accept=[BlockerCode.RECENT_ACTIVITY])
