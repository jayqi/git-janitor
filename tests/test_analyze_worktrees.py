import shutil

from git_janitor.analysis import analyze
from git_janitor.model import (
    DeleteBranch,
    DirtyBlocker,
    InProgressOpBlocker,
    IsCurrentBlocker,
    IsMainBlocker,
    LockedBlocker,
    PreciousFilesBlocker,
    RemoveWorktree,
    StashesBlocker,
    Status,
    UntrackedBlocker,
    Verdict,
)


def merged_branch_in_worktree(sandbox, name="feat"):
    path = sandbox.add_worktree(name, create=True)
    sandbox.commit(cwd=path)
    pr = sandbox.open_pr(name)
    sandbox.squash_merge(pr)
    return path


def test_merged_branch_in_clean_worktree_is_deletable(sandbox):
    path = merged_branch_in_worktree(sandbox)

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.status is Status.MERGED
    assert candidate.verdict is Verdict.DELETE
    assert candidate.worktree.path == path
    assert candidate.blockers == ()
    assert candidate.actions == (
        RemoveWorktree(path=path),
        DeleteBranch(name="feat", tip=sandbox.tip("feat")),
    )


def blocker(candidate, kind):
    [found] = [b for b in candidate.blockers if isinstance(b, kind)]
    return found


def test_worktree_with_modified_files_needs_review(sandbox):
    path = merged_branch_in_worktree(sandbox)
    (path / "file1.txt").write_text("changed\n")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.verdict is Verdict.REVIEW
    assert blocker(candidate, DirtyBlocker).paths == ("file1.txt",)


def test_worktree_with_untracked_files_needs_review(sandbox):
    path = merged_branch_in_worktree(sandbox)
    (path / "notes").mkdir()
    (path / "notes" / "todo.txt").write_text("todo\n")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.verdict is Verdict.REVIEW
    assert blocker(candidate, UntrackedBlocker).paths == ("notes/todo.txt",)


def test_ignored_files_from_global_excludes_are_precious(sandbox, global_excludes):
    global_excludes.write_text(".plans/\n.DS_Store\n")
    path = sandbox.add_worktree("feat", create=True)
    (path / ".gitignore").write_text(".venv/\n")
    sandbox.git("add", ".gitignore", cwd=path)
    sandbox.git("commit", "-m", "Ignore venv", cwd=path)
    sandbox.squash_merge(sandbox.open_pr("feat"))
    (path / ".plans" / "sub").mkdir(parents=True)
    (path / ".plans" / "plan.md").write_text("plan\n")
    (path / ".plans" / "sub" / "notes.md").write_text("notes!\n")
    (path / ".plans" / ".DS_Store").write_text("")
    (path / ".venv" / "lib").mkdir(parents=True)
    (path / ".venv" / "lib" / "module.py").write_text("")
    (path / ".DS_Store").write_text("")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.verdict is Verdict.REVIEW
    files = blocker(candidate, PreciousFilesBlocker).files
    assert [(f.path, f.size) for f in files] == [(".plans/plan.md", 5), (".plans/sub/notes.md", 7)]


def test_ignored_files_from_info_exclude_are_precious(sandbox):
    path = merged_branch_in_worktree(sandbox)
    with (sandbox.repo / ".git" / "info" / "exclude").open("a") as f:
        f.write("scratch.txt\n")
    (path / "scratch.txt").write_text("scratch\n")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    files = blocker(candidate, PreciousFilesBlocker).files
    assert [f.path for f in files] == ["scratch.txt"]


def test_configured_disposable_patterns_are_not_precious(sandbox, global_excludes):
    global_excludes.write_text(".envrc\n")
    sandbox.git("config", "--add", "janitor.disposable", ".envrc")
    path = merged_branch_in_worktree(sandbox)
    (path / ".envrc").write_text("export X=1\n")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.blockers == ()
    assert candidate.verdict is Verdict.DELETE


def test_locked_worktree_needs_review(sandbox):
    path = merged_branch_in_worktree(sandbox)
    sandbox.git("worktree", "lock", str(path))

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.verdict is Verdict.REVIEW
    assert blocker(candidate, LockedBlocker)


def test_worktree_with_merge_in_progress_needs_review(sandbox):
    path = merged_branch_in_worktree(sandbox)
    sandbox.new_branch("other")
    sandbox.git("merge", "--no-commit", "--no-ff", "other", cwd=path)

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.verdict is Verdict.REVIEW
    assert blocker(candidate, InProgressOpBlocker).operation == "merge"


def test_worktree_containing_current_directory_needs_review(sandbox):
    path = merged_branch_in_worktree(sandbox)

    report = analyze(sandbox.repo, sandbox.github, cwd=path / "subdir")

    candidate = report.candidate("feat")
    assert candidate.verdict is Verdict.REVIEW
    assert blocker(candidate, IsCurrentBlocker)


def test_merged_branch_checked_out_in_main_worktree_needs_review(sandbox):
    sandbox.new_branch("feat")
    sandbox.squash_merge(sandbox.open_pr("feat"))
    sandbox.git("checkout", "-q", "feat")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.status is Status.MERGED
    assert candidate.verdict is Verdict.REVIEW
    assert blocker(candidate, IsMainBlocker)
    assert candidate.actions == (DeleteBranch(name="feat", tip=sandbox.tip("feat")),)


def test_branch_with_stashes_needs_review(sandbox):
    sandbox.new_branch("feat")
    sandbox.squash_merge(sandbox.open_pr("feat"))
    sandbox.git("checkout", "-q", "feat")
    (sandbox.repo / "file1.txt").write_text("unsaved\n")
    sandbox.git("stash", "push", "-m", "half-done idea")
    sandbox.git("checkout", "-q", "main")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.verdict is Verdict.REVIEW
    [stash] = blocker(candidate, StashesBlocker).stashes
    assert stash.ref == "stash@{0}"
    assert stash.subject == "On feat: half-done idea"


def test_detached_worktree_is_its_own_candidate(sandbox):
    path = sandbox.add_detached_worktree("scratch")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate(str(path))
    assert candidate.branch is None
    assert candidate.worktree.path == path
    assert candidate.status is Status.NO_UNIQUE_COMMITS
    assert candidate.verdict is Verdict.REVIEW
    assert candidate.actions == (RemoveWorktree(path=path),)


def test_detached_worktree_with_new_commits_has_no_pr(sandbox):
    path = sandbox.add_detached_worktree("scratch")
    sandbox.commit(cwd=path)

    report = analyze(sandbox.repo, sandbox.github)

    assert report.candidate(str(path)).status is Status.NO_PR


def test_merged_branch_in_missing_worktree_is_deletable(sandbox):
    path = merged_branch_in_worktree(sandbox)
    shutil.rmtree(path)

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.worktree.prunable
    assert candidate.verdict is Verdict.DELETE
    assert candidate.actions == (
        RemoveWorktree(path=path),
        DeleteBranch(name="feat", tip=sandbox.tip("feat")),
    )


def test_linked_worktree_on_default_branch_needs_review(sandbox):
    sandbox.new_branch("feat")
    sandbox.git("checkout", "-q", "feat")
    path = sandbox.add_worktree("main")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("main")
    assert candidate.status is Status.DEFAULT_BRANCH
    assert candidate.verdict is Verdict.REVIEW
    assert candidate.actions == (RemoveWorktree(path=path),)
