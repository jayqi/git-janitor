import pytest

from git_janitor.analysis import Verdict, analyze
from git_janitor.clean import Outcome, clean


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
    assert outcome.recovery == [f"git branch feat {tip}"]
    assert not path.exists()
    assert "feat" not in local_branches(sandbox)


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
