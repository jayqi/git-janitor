import pytest

from git_janitor.analysis import analyze
from git_janitor.git import GitError
from git_janitor.model import (
    DeleteBranch,
    ExtraCommits,
    PullRequestEvidence,
    Status,
    Verdict,
)


def test_squash_merged_branch_is_deletable(sandbox):
    sandbox.new_branch("feat", commits=2)
    pr = sandbox.open_pr("feat")
    sandbox.squash_merge(pr)

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.status is Status.MERGED
    assert candidate.verdict is Verdict.DELETE
    assert candidate.actions == (DeleteBranch(name="feat", tip=sandbox.tip("feat")),)


def test_branch_behind_its_merged_pull_request_is_deletable(sandbox):
    tip = sandbox.new_branch("feat")
    pr = sandbox.open_pr("feat")
    sandbox.push_to_pr_from_elsewhere(pr)
    sandbox.squash_merge(pr)

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.branch.tip == tip
    assert candidate.status is Status.MERGED
    assert candidate.verdict is Verdict.DELETE


def test_branch_with_commits_after_its_merged_pull_request_needs_review(sandbox):
    sandbox.new_branch("feat")
    pr = sandbox.open_pr("feat")
    sandbox.squash_merge(pr)
    extra = sandbox.add_commits("feat", commits=2)

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.status is Status.MERGED_PLUS_LOCAL
    assert candidate.verdict is Verdict.REVIEW
    [evidence] = [e for e in candidate.evidence if isinstance(e, ExtraCommits)]
    assert evidence.pull_request == pr
    assert [commit.sha for commit in evidence.commits] == extra
    assert [commit.subject for commit in evidence.commits] == ["Commit 3", "Commit 4"]


def test_branch_with_open_pull_request_is_kept(sandbox):
    sandbox.new_branch("feat")
    sandbox.open_pr("feat")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.status is Status.PR_OPEN
    assert candidate.verdict is Verdict.KEEP


def test_branch_with_closed_pull_request_needs_review(sandbox):
    sandbox.new_branch("feat")
    pr = sandbox.open_pr("feat")
    sandbox.close_pr(pr)

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.status is Status.PR_CLOSED
    assert candidate.verdict is Verdict.REVIEW


def test_branch_merged_into_another_branch_needs_review(sandbox):
    sandbox.new_branch("base-feat")
    sandbox.push("base-feat")
    sandbox.new_branch("feat", start="base-feat")
    pr = sandbox.open_pr("feat", base="base-feat")
    sandbox.squash_merge(pr)

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.status is Status.MERGED_INTO_OTHER_BASE
    assert candidate.verdict is Verdict.REVIEW


def test_branch_without_commits_of_its_own_or_worktree_is_deletable(sandbox):
    sandbox.git("branch", "fresh", "main")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("fresh")
    assert candidate.status is Status.NO_UNIQUE_COMMITS
    assert candidate.verdict is Verdict.DELETE


def test_default_branch_is_kept_and_never_deleted(sandbox):
    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("main")
    assert candidate.status is Status.DEFAULT_BRANCH
    assert candidate.verdict is Verdict.KEEP
    assert candidate.actions == ()


def test_pull_request_is_found_by_upstream_name(sandbox):
    sandbox.new_branch("feat")
    pr = sandbox.open_pr("feat")
    sandbox.git("branch", "-m", "feat", "my-feat")
    sandbox.squash_merge(pr)

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("my-feat")
    assert candidate.status is Status.MERGED
    assert candidate.verdict is Verdict.DELETE


def test_most_recent_matching_pull_request_decides_status(sandbox):
    sandbox.new_branch("feat")
    first = sandbox.open_pr("feat")
    sandbox.close_pr(first)
    second = sandbox.open_pr("feat")
    sandbox.squash_merge(second)

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.status is Status.MERGED
    assert [(e.pull_request.number, e.matched) for e in candidate.evidence] == [
        (second, True),
        (first, True),
    ]


def test_fresh_branch_reusing_merged_pull_request_name_has_no_unique_commits(sandbox):
    sandbox.new_branch("fix")
    sandbox.squash_merge(sandbox.open_pr("fix"))
    sandbox.git("branch", "-D", "fix")
    sandbox.git("branch", "fix", "main")  # Local main predates the squash commit

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("fix")
    assert candidate.status is Status.NO_UNIQUE_COMMITS


def test_unfetchable_pull_request_head_does_not_count_as_match(sandbox):
    sandbox.new_branch("feat")
    pr = sandbox.open_pr("feat")
    sandbox.push_to_pr_from_elsewhere(pr)
    sandbox.squash_merge(pr)
    sandbox.git("update-ref", "-d", f"refs/pull/{pr}/head", cwd=sandbox.origin)

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.status is Status.NO_PR
    assert candidate.verdict is Verdict.REVIEW


def test_commit_messages_with_unicode_line_separators(sandbox):
    sandbox.new_branch("feat")
    pr = sandbox.open_pr("feat")
    sandbox.squash_merge(pr)
    sandbox.git("checkout", "-q", "feat")
    sandbox.commit(message="Before after")
    sandbox.git("checkout", "-q", "main")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    [extra] = [e for e in candidate.evidence if isinstance(e, ExtraCommits)]
    assert [c.subject for c in extra.commits] == ["Before after"]


def test_missing_default_branch_ref_is_an_error(sandbox):
    sandbox.git("update-ref", "-d", "refs/remotes/origin/main")

    with pytest.raises(GitError, match="refs/remotes/origin/main"):
        analyze(sandbox.repo, sandbox.github, fetch=False)


def test_open_pull_request_with_unpushed_commits_is_kept(sandbox):
    sandbox.new_branch("feat")
    pr = sandbox.open_pr("feat")
    extra = sandbox.add_commits("feat")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.status is Status.PR_OPEN
    assert candidate.verdict is Verdict.KEEP
    [evidence] = [e for e in candidate.evidence if isinstance(e, ExtraCommits)]
    assert evidence.pull_request == pr
    assert [c.sha for c in evidence.commits] == extra


def test_any_matching_open_pull_request_keeps_branch(sandbox):
    sandbox.new_branch("feat")
    sandbox.open_pr("feat", base="release")
    sandbox.squash_merge(sandbox.open_pr("feat"))

    report = analyze(sandbox.repo, sandbox.github)

    assert report.candidate("feat").status is Status.PR_OPEN


def test_extra_commits_exclude_default_branch_commits(sandbox):
    sandbox.new_branch("feat")
    sandbox.squash_merge(sandbox.open_pr("feat"))
    sandbox.git("fetch", "-q", "origin")
    sandbox.git("checkout", "-q", "feat")
    sandbox.git("merge", "-q", "--no-ff", "-m", "Merge main into feat", "origin/main")
    sandbox.git("checkout", "-q", "main")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.status is Status.MERGED_PLUS_LOCAL
    [evidence] = [e for e in candidate.evidence if isinstance(e, ExtraCommits)]
    assert [c.subject for c in evidence.commits] == ["Merge main into feat"]


def test_merge_commit_merged_branch_is_deletable(sandbox):
    sandbox.new_branch("feat")
    sandbox.merge_commit_merge(sandbox.open_pr("feat"))

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.status is Status.MERGED
    assert candidate.verdict is Verdict.DELETE


def test_branch_fast_forwarded_to_default_branch_has_no_unique_commits(sandbox):
    sandbox.new_branch("feat")
    pr = sandbox.open_pr("feat")
    sandbox.merge_commit_merge(pr)
    sandbox.git("fetch", "-q", "origin")
    sandbox.git("branch", "-f", "feat", "origin/main")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.status is Status.NO_UNIQUE_COMMITS
    assert candidate.evidence == (
        PullRequestEvidence(pull_request=sandbox.github.get(pr), matched=False),
    )


def test_pull_request_head_missing_after_fetch_does_not_count_as_match(sandbox):
    sandbox.new_branch("feat")
    pr = sandbox.open_pr("feat")
    sandbox.add_commits("feat")
    sandbox.github.update(pr, head_oid="1" * 40)  # GitHub's data disagrees with refs/pull

    report = analyze(sandbox.repo, sandbox.github)

    assert report.candidate("feat").status is Status.NO_PR
