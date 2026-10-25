from git_janitor.analysis import analyze
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


def test_reused_branch_name_does_not_inherit_old_merged_pull_request(sandbox):
    sandbox.new_branch("feat")
    old_pr = sandbox.open_pr("feat")
    sandbox.squash_merge(old_pr)
    sandbox.git("branch", "-D", "feat")
    sandbox.git("fetch", "-q", "origin")
    sandbox.new_branch("feat", start="origin/main")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("feat")
    assert candidate.status is Status.NO_PR
    assert candidate.verdict is Verdict.REVIEW
    assert candidate.evidence == (
        PullRequestEvidence(pull_request=sandbox.github.get(old_pr), matched=False),
    )


def test_branch_without_commits_of_its_own_needs_review(sandbox):
    sandbox.git("branch", "fresh", "main")

    report = analyze(sandbox.repo, sandbox.github)

    candidate = report.candidate("fresh")
    assert candidate.status is Status.NO_UNIQUE_COMMITS
    assert candidate.verdict is Verdict.REVIEW


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
