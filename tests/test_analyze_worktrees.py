from datetime import UTC, datetime, timedelta
import os
import shutil

import pytest

from git_janitor.analysis import analyze
from git_janitor.model import (
    DeleteBranch,
    ErrorBlocker,
    InProgressOpBlocker,
    IsCurrentBlocker,
    IsMainBlocker,
    LockedBlocker,
    NestedRepositoryBlocker,
    PreciousFilesBlocker,
    PullRequestEvidence,
    RecentActivityBlocker,
    RemoveWorktree,
    Status,
    UntrackedBlocker,
    Verdict,
)


def blocker(candidate, kind):
    [found] = [b for b in candidate.blockers if isinstance(b, kind)]
    return found


def test_worktree_with_untracked_files_needs_review(sandbox):
    path, _ = sandbox.merged_worktree()
    (path / "notes").mkdir()
    (path / "notes" / "todo.txt").write_text("todo\n")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.verdict is Verdict.REVIEW
    assert blocker(candidate, UntrackedBlocker).paths == ("notes/todo.txt",)


def test_ignored_files_from_global_excludes_are_precious(sandbox, global_excludes):
    global_excludes.write_text(".plans/\n.DS_Store\n*.swp\n")
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
    (path / ".notes.md.swp").write_text("unsaved\n")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.verdict is Verdict.REVIEW
    files = blocker(candidate, PreciousFilesBlocker).files
    assert [(f.path, f.size) for f in files] == [
        (".notes.md.swp", 8),
        (".plans/plan.md", 5),
        (".plans/sub/notes.md", 7),
    ]


def test_ignored_files_from_info_exclude_are_precious(sandbox):
    path, _ = sandbox.merged_worktree()
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
    path, _ = sandbox.merged_worktree()
    (path / ".envrc").write_text("export X=1\n")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.blockers == ()
    assert candidate.verdict is Verdict.DELETE


def test_worktree_containing_current_directory_needs_review(sandbox):
    path, _ = sandbox.merged_worktree()

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


def test_detached_worktree_with_new_commits_has_no_pr(sandbox):
    path = sandbox.add_detached_worktree("scratch")
    sandbox.commit(cwd=path)

    report = analyze(sandbox.repo, sandbox.github)

    assert report.candidate(str(path)).status is Status.NO_PR


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
    path, _ = sandbox.merged_worktree()
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


def test_repository_nested_in_disposable_directory_needs_review(sandbox):
    path = sandbox.add_worktree("feat", create=True)
    commit_gitignore(sandbox, path, "vendor/\n")
    sandbox.squash_merge(sandbox.open_pr("feat"))
    nested = path / "vendor" / "lib"
    sandbox.git("init", "-q", str(nested))
    (nested / "lib.py").write_text("work\n")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.blockers == (NestedRepositoryBlocker(paths=("vendor/lib",)),)


def test_untracked_gitignore_does_not_make_files_disposable(sandbox):
    path, _ = sandbox.merged_worktree()
    (path / "notes").mkdir()
    (path / "notes" / ".gitignore").write_text("*\n")
    (path / "notes" / "ideas.md").write_text("ideas\n")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    files = blocker(candidate, PreciousFilesBlocker).files
    assert [f.path for f in files] == ["notes/.gitignore", "notes/ideas.md"]


def test_locked_worktree_with_missing_directory_needs_review(sandbox):
    path, _ = sandbox.merged_worktree()
    sandbox.git("worktree", "lock", str(path))
    shutil.rmtree(path)

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.verdict is Verdict.REVIEW
    assert candidate.blockers == (LockedBlocker(),)


def test_analysis_does_not_rewrite_worktree_index(sandbox):
    path, _ = sandbox.merged_worktree()
    index = sandbox.repo / ".git" / "worktrees" / path.name / "index"
    os.utime(path / "file2.txt", (1_900_000_000, 1_900_000_000))
    before = index.stat().st_mtime_ns

    analyze(sandbox.repo, sandbox.github)

    assert index.stat().st_mtime_ns == before


CACHEDIR_TAG = "Signature: 8a477f597d28d172789f06886806bc55\n# A cache directory.\n"


def test_cache_directories_are_disposable(sandbox, global_excludes):
    global_excludes.write_text(".plans/\n")
    path, _ = sandbox.merged_worktree()
    for cache in (path / ".ruff_cache", path / ".plans" / "cache"):
        cache.mkdir(parents=True)
        (cache / "CACHEDIR.TAG").write_text(CACHEDIR_TAG)
        (cache / ".gitignore").write_text("*\n")
        (cache / "data.bin").write_text("cached\n")
    (path / ".plans" / "plan.md").write_text("plan\n")

    report = analyze(sandbox.repo, sandbox.github)

    files = blocker(report.candidate("feat"), PreciousFilesBlocker).files
    assert [f.path for f in files] == [".plans/plan.md"]


def test_repository_nested_in_precious_directory_is_reported_once(sandbox, global_excludes):
    global_excludes.write_text(".plans/\n")
    path, _ = sandbox.merged_worktree()
    sandbox.git("init", "-q", str(path / ".plans" / "experiment"))
    (path / ".plans" / "experiment" / "work.py").write_text("work\n")
    (path / ".plans" / "plan.md").write_text("plan\n")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.blockers[0] == NestedRepositoryBlocker(paths=(".plans/experiment",))
    assert [f.path for f in blocker(candidate, PreciousFilesBlocker).files] == [".plans/plan.md"]


def days_from_now(days):
    return datetime.now(UTC) + timedelta(days=days)


def test_recently_created_worktree_without_unique_commits_needs_review(sandbox):
    path = sandbox.add_worktree("fresh", create=True)

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("fresh")
    assert candidate.status is Status.NO_UNIQUE_COMMITS
    assert candidate.verdict is Verdict.REVIEW
    assert candidate.blockers == (RecentActivityBlocker(min_age=timedelta(days=7)),)
    assert candidate.worktree.path == path


def test_inactive_worktree_without_unique_commits_is_deletable(sandbox):
    path = sandbox.add_worktree("fresh", create=True)

    report = analyze(sandbox.repo, sandbox.github, now=days_from_now(8))

    candidate = report.candidate("fresh")
    assert candidate.verdict is Verdict.DELETE
    assert candidate.actions == (
        RemoveWorktree(path=path),
        DeleteBranch(name="fresh", tip=sandbox.tip("main")),
    )


def test_detached_worktree_in_default_branch_is_deletable_once_inactive(sandbox):
    path = sandbox.add_detached_worktree("scratch")

    recent = analyze(sandbox.repo, sandbox.github).candidate(str(path))
    inactive = analyze(sandbox.repo, sandbox.github, now=days_from_now(8)).candidate(str(path))

    assert recent.verdict is Verdict.REVIEW
    assert inactive.verdict is Verdict.DELETE


def test_minimum_age_is_configurable(sandbox):
    sandbox.add_worktree("fresh", create=True)
    sandbox.git("config", "janitor.minAge", "2h")

    report = analyze(sandbox.repo, sandbox.github, now=datetime.now(UTC) + timedelta(hours=3))

    assert report.candidate("fresh").verdict is Verdict.DELETE


def test_invalid_minimum_age_is_an_error(sandbox):
    sandbox.git("config", "janitor.minAge", "soon")

    with pytest.raises(ValueError, match="janitor.minAge"):
        analyze(sandbox.repo, sandbox.github)


def test_last_activity_of_detached_worktree_is_its_latest_checkout(sandbox, monkeypatch):
    tip = sandbox.new_branch("feat")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2020-01-01T00:00:00Z")
    path = sandbox.add_detached_worktree("review")
    commondir = sandbox.repo / ".git" / "worktrees" / path.name / "commondir"
    os.utime(commondir, (1_577_836_800, 1_577_836_800))
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2021-01-01T00:00:00Z")
    sandbox.git("checkout", "-q", "--detach", tip, cwd=path)

    report = analyze(sandbox.repo, sandbox.github)

    assert report.candidate(str(path)).last_activity == datetime(2021, 1, 1, tzinfo=UTC)


def test_last_activity_of_branch_without_worktree(sandbox):
    before = datetime.now(UTC).replace(microsecond=0)
    sandbox.new_branch("feat")

    report = analyze(sandbox.repo, sandbox.github)

    assert before <= report.candidate("feat").last_activity <= datetime.now(UTC)


def detached_worktree_at_pull_request_head(sandbox):
    sandbox.new_branch("feat")
    pr = sandbox.open_pr("feat")
    head = sandbox.tip("feat")
    sandbox.git("branch", "-D", "feat")
    return sandbox.add_detached_worktree("review", commit=head), pr


def test_detached_worktree_at_merged_pull_request_head_is_deletable(sandbox):
    path, pr = detached_worktree_at_pull_request_head(sandbox)
    sandbox.squash_merge(pr)

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate(str(path))
    assert candidate.status is Status.MERGED
    assert candidate.verdict is Verdict.DELETE
    assert candidate.evidence == (
        PullRequestEvidence(pull_request=sandbox.github.get(pr), matched=True),
    )


def test_detached_worktree_at_open_pull_request_head_is_kept(sandbox):
    path, _ = detached_worktree_at_pull_request_head(sandbox)

    report = analyze(sandbox.repo, sandbox.github)

    assert report.candidate(str(path)).status is Status.PR_OPEN


def test_detached_worktree_at_intermediate_pull_request_commit_is_merged(sandbox):
    sandbox.new_branch("feat", commits=2)
    pr = sandbox.open_pr("feat")
    first = sandbox.tip("feat~1")
    sandbox.git("branch", "-D", "feat")
    path = sandbox.add_detached_worktree("review", commit=first)
    sandbox.squash_merge(pr)

    report = analyze(sandbox.repo, sandbox.github)

    assert report.candidate(str(path)).status is Status.MERGED


def test_detached_worktree_at_squash_commit_has_no_unique_commits(sandbox):
    sandbox.new_branch("feat")
    pr = sandbox.open_pr("feat")
    squash = sandbox.squash_merge(pr)
    sandbox.git("fetch", "-q", "origin")
    path = sandbox.add_detached_worktree("main-copy", commit=squash)

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate(str(path))
    assert candidate.status is Status.NO_UNIQUE_COMMITS
    assert candidate.evidence == (
        PullRequestEvidence(pull_request=sandbox.github.get(pr), matched=False),
    )


def old_worktree(sandbox, monkeypatch, name="fresh"):
    """A worktree with no commits of its own, created and last committed to in 2020."""
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2020-01-01T00:00:00Z")
    path = sandbox.add_worktree(name, create=True)
    monkeypatch.delenv("GIT_COMMITTER_DATE")
    commondir = sandbox.repo / ".git" / "worktrees" / path.name / "commondir"
    os.utime(commondir, (1_577_836_800, 1_577_836_800))
    return path


def test_git_status_does_not_count_as_activity(sandbox, monkeypatch):
    path = old_worktree(sandbox, monkeypatch)
    os.utime(path / "file1.txt", (1_900_000_000, 1_900_000_000))
    sandbox.git("status", cwd=path)  # Refreshes and rewrites the index

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("fresh")
    assert candidate.last_activity == datetime(2020, 1, 1, tzinfo=UTC)
    assert candidate.verdict is Verdict.DELETE


def test_worktree_git_cannot_read_gets_error_blocker(sandbox):
    path, _ = sandbox.merged_worktree()
    (sandbox.repo / ".git" / "worktrees" / path.name / "index").write_text("not an index")

    report = analyze(sandbox.repo, sandbox.github)

    [error] = report.candidate("feat").blockers
    assert isinstance(error, ErrorBlocker)
    assert "index" in error.detail
    assert report.candidate("main").verdict is Verdict.KEEP
