import os
import shutil

from git_janitor.analysis import analyze
from git_janitor.model import (
    BrokenWorktreeBlocker,
    DeleteBranch,
    DirtyBlocker,
    InProgressOpBlocker,
    IsCurrentBlocker,
    IsMainBlocker,
    LockedBlocker,
    MultipleWorktreesBlocker,
    NestedWorktreeBlocker,
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


def test_worktree_mid_rebase_stays_with_its_branch(sandbox):
    path = merged_branch_in_worktree(sandbox)
    sandbox.git(
        "-c", "sequence.editor=sed -i.bak 1s/^pick/edit/", "rebase", "-q", "-i", "HEAD~1", cwd=path
    )

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.worktree.path == path
    assert blocker(candidate, InProgressOpBlocker).operation == "rebase"
    assert candidate.verdict is Verdict.REVIEW
    assert [c.id for c in report.candidates] == ["feat", "main"]


def test_worktree_mid_bisect_stays_with_its_branch(sandbox):
    path = sandbox.add_worktree("feat", create=True)
    for _ in range(3):
        sandbox.commit(cwd=path)
    sandbox.squash_merge(sandbox.open_pr("feat"))
    sandbox.git("bisect", "start", "HEAD", "HEAD~2", cwd=path)

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.worktree.path == path
    assert blocker(candidate, InProgressOpBlocker).operation == "bisect"
    assert [c.id for c in report.candidates] == ["feat", "main"]


def commit_gitignore(sandbox, path, content):
    (path / ".gitignore").write_text(content)
    sandbox.git("add", ".gitignore", cwd=path)
    sandbox.git("commit", "-m", "Add .gitignore", cwd=path)


def test_worktree_containing_another_worktree_needs_review(sandbox):
    path = sandbox.add_worktree("outer", create=True)
    commit_gitignore(sandbox, path, ".worktrees/\n")
    sandbox.squash_merge(sandbox.open_pr("outer"))
    inner = path / ".worktrees" / "inner"
    sandbox.git("worktree", "add", "-q", "-b", "inner", str(inner), "main")
    (inner / "wip.txt").write_text("wip\n")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("outer")
    assert candidate.verdict is Verdict.REVIEW
    assert candidate.blockers == (NestedWorktreeBlocker(paths=(".worktrees/inner",)),)


def test_repository_nested_in_ignored_directory_is_precious(sandbox):
    path = sandbox.add_worktree("feat", create=True)
    commit_gitignore(sandbox, path, "vendor/\n")
    sandbox.squash_merge(sandbox.open_pr("feat"))
    nested = path / "vendor" / "lib"
    sandbox.git("init", "-q", str(nested))
    (nested / "lib.py").write_text("work\n")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    files = blocker(candidate, PreciousFilesBlocker).files
    assert [f.path for f in files] == ["vendor/lib/lib.py"]


def test_untracked_gitignore_does_not_make_files_disposable(sandbox):
    path = merged_branch_in_worktree(sandbox)
    (path / "notes").mkdir()
    (path / "notes" / ".gitignore").write_text("*\n")
    (path / "notes" / "ideas.md").write_text("ideas\n")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    files = blocker(candidate, PreciousFilesBlocker).files
    assert [f.path for f in files] == ["notes/.gitignore", "notes/ideas.md"]


def test_branch_checked_out_in_two_worktrees_needs_review(sandbox):
    first = merged_branch_in_worktree(sandbox)
    second = sandbox.root / "wt-feat-again"
    sandbox.git("worktree", "add", "-q", "-f", str(second), "feat")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.verdict is Verdict.REVIEW
    assert blocker(candidate, MultipleWorktreesBlocker).paths == (str(first), str(second))
    assert [c.id for c in report.candidates] == ["feat", "main"]


def test_worktree_no_longer_valid_but_still_present_needs_review(sandbox):
    path = merged_branch_in_worktree(sandbox)
    (path / ".git").unlink()

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.worktree.prunable
    assert candidate.verdict is Verdict.REVIEW
    assert candidate.blockers == (BrokenWorktreeBlocker(),)


def test_locked_worktree_with_missing_directory_needs_review(sandbox):
    path = merged_branch_in_worktree(sandbox)
    sandbox.git("worktree", "lock", str(path))
    shutil.rmtree(path)

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.verdict is Verdict.REVIEW
    assert candidate.blockers == (LockedBlocker(),)


def test_broken_symlink_in_precious_directory_is_reported(sandbox, global_excludes):
    global_excludes.write_text(".plans/\n")
    path = merged_branch_in_worktree(sandbox)
    (path / ".plans").mkdir()
    (path / ".plans" / "link").symlink_to("/nonexistent/target")

    report = analyze(sandbox.repo, sandbox.github)

    files = blocker(report.candidate("feat"), PreciousFilesBlocker).files
    assert [f.path for f in files] == [".plans/link"]


def test_analysis_does_not_rewrite_worktree_index(sandbox):
    path = merged_branch_in_worktree(sandbox)
    index = sandbox.repo / ".git" / "worktrees" / path.name / "index"
    os.utime(path / "file2.txt", (1_900_000_000, 1_900_000_000))
    before = index.stat().st_mtime_ns

    analyze(sandbox.repo, sandbox.github)

    assert index.stat().st_mtime_ns == before
