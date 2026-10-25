import shutil

import pytest

from git_janitor import clean as clean_module
from git_janitor import git
from git_janitor.analysis import analyze
from git_janitor.clean import clean
from git_janitor.model import Outcome, Verdict


def merged_branch_in_worktree(sandbox, name="feat"):
    path = sandbox.add_worktree(name, create=True)
    sandbox.commit(cwd=path)
    sandbox.squash_merge(sandbox.open_pr(name))
    return path


def local_branches(sandbox):
    return sandbox.git("branch", "--format=%(refname:short)").splitlines()


def test_clean_removes_worktree_and_branch(sandbox):
    path = merged_branch_in_worktree(sandbox)
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
    path = merged_branch_in_worktree(sandbox)

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


def test_clean_reanalyzes_instead_of_trusting_an_earlier_report(sandbox):
    path = merged_branch_in_worktree(sandbox)
    assert analyze(sandbox.repo, sandbox.github).candidate("feat").verdict is Verdict.DELETE
    (path / "new-work.txt").write_text("unsaved\n")

    result = clean(sandbox.repo, sandbox.github, targets=["feat"])

    assert result.outcomes[0].outcome is Outcome.REFUSED
    assert (path / "new-work.txt").exists()


def test_clean_reports_unknown_targets(sandbox):
    result = clean(sandbox.repo, sandbox.github, targets=["nope"])

    assert result.outcomes[0].outcome is Outcome.NOT_FOUND


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
    path = merged_branch_in_worktree(sandbox)

    result = clean(sandbox.repo, sandbox.github, targets=["feat"], dry_run=True)

    assert result.outcomes[0].outcome is Outcome.WOULD_DELETE
    assert path.exists()
    assert "feat" in local_branches(sandbox)


def test_clean_requires_targets_or_all(sandbox):
    with pytest.raises(ValueError):
        clean(sandbox.repo, sandbox.github)


def test_clean_failure_reports_recovery_only_for_completed_actions(sandbox, unremovable_worktree):
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


def test_clean_keeps_branch_whose_worktree_got_new_commits(sandbox, after_analysis):
    path = merged_branch_in_worktree(sandbox)
    new_commits = []
    after_analysis(lambda: new_commits.append(sandbox.commit(cwd=path)))

    result = clean(sandbox.repo, sandbox.github, targets=["feat"])

    [outcome] = result.outcomes
    assert outcome.outcome is Outcome.FAILED
    assert "changed since the analysis" in outcome.detail
    assert path.exists()
    assert sandbox.tip("feat") == new_commits[0]


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
    path = merged_branch_in_worktree(sandbox)

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


def test_clean_runs_each_candidate_once(sandbox):
    path = merged_branch_in_worktree(sandbox)

    result = clean(sandbox.repo, sandbox.github, targets=["feat", str(path)])

    assert [(o.id, o.outcome) for o in result.outcomes] == [("feat", Outcome.DELETED)]


def test_clean_rejects_targets_with_all(sandbox):
    with pytest.raises(ValueError):
        clean(sandbox.repo, sandbox.github, targets=["feat"], all=True)


def test_clean_keeps_worktree_that_switched_branches(sandbox, after_analysis):
    path = merged_branch_in_worktree(sandbox)
    after_analysis(lambda: sandbox.git("switch", "-q", "-c", "agent-task", cwd=path))

    result = clean(sandbox.repo, sandbox.github, targets=["feat"])

    assert result.outcomes[0].outcome is Outcome.FAILED
    assert "changed since the analysis" in result.outcomes[0].detail
    assert path.exists()


def test_clean_removes_branch_whose_worktree_directory_is_missing(sandbox):
    path = merged_branch_in_worktree(sandbox)
    shutil.rmtree(path)

    result = clean(sandbox.repo, sandbox.github, targets=["feat"])

    assert result.outcomes[0].outcome is Outcome.DELETED
    assert "feat" not in local_branches(sandbox)
    assert "wt-feat" not in sandbox.git("worktree", "list")


def test_clean_reports_filesystem_errors_and_continues(sandbox, after_analysis, monkeypatch):
    first = merged_branch_in_worktree(sandbox, "first")
    merged_branch_in_worktree(sandbox, "second")
    worktree_status = git.worktree_status

    def fail_for_first(cwd):
        if cwd == first:
            raise PermissionError(13, "Permission denied", str(cwd))
        return worktree_status(cwd)

    after_analysis(lambda: monkeypatch.setattr(git, "worktree_status", fail_for_first))

    result = clean(sandbox.repo, sandbox.github, all=True)

    assert [(o.id, o.outcome) for o in result.outcomes] == [
        ("first", Outcome.FAILED),
        ("second", Outcome.DELETED),
    ]
    assert "Permission denied" in result.outcomes[0].detail
