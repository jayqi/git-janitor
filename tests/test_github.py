import json

import pytest

from git_janitor.github import GhGitHubClient, GitHubError, run_gh
from git_janitor.model import PullRequest, PullRequestState


class FakeGh:
    """Stands in for the `gh` executable, answering GraphQL queries from canned pull requests."""

    def __init__(self, prs_by_head=None, default_branch="main"):
        self.prs_by_head = prs_by_head or {}
        self.default_branch = default_branch
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        fields = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "-f")
        repository = {
            "nameWithOwner": f"{fields['owner']}/{fields['name']}",
            "defaultBranchRef": self.default_branch and {"name": self.default_branch},
        }
        for key, head in fields.items():
            if key.startswith("head"):
                alias = key.replace("head", "pr")
                repository[alias] = {"nodes": self.prs_by_head.get(head, [])}
        return json.dumps({"data": {"repository": repository}})

    def variables(self, call):
        args = self.calls[call]
        return dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "-f")


def pr_node(number, head, state="MERGED", base="main", oid="abc123"):
    return {
        "number": number,
        "url": f"https://github.com/owner/repo/pull/{number}",
        "state": state,
        "baseRefName": base,
        "headRefName": head,
        "headRefOid": oid,
    }


def test_fetch_returns_default_branch_and_pull_requests_by_head():
    gh = FakeGh(
        {"feat": [pr_node(2, "feat", state="OPEN", oid="def456"), pr_node(1, "feat")]},
        default_branch="trunk",
    )
    client = GhGitHubClient(run_gh=gh)

    data = client.fetch("git@github.com:owner/repo.git", ["feat", "other"])

    assert data.name_with_owner == "owner/repo"
    assert data.default_branch == "trunk"
    assert data.pull_requests == {
        "feat": [
            PullRequest(
                number=2,
                url="https://github.com/owner/repo/pull/2",
                state=PullRequestState.OPEN,
                base="main",
                head_name="feat",
                head_oid="def456",
            ),
            PullRequest(
                number=1,
                url="https://github.com/owner/repo/pull/1",
                state=PullRequestState.MERGED,
                base="main",
                head_name="feat",
                head_oid="abc123",
            ),
        ],
        "other": [],
    }


def test_fetch_queries_heads_in_batches():
    heads = [f"branch-{i}" for i in range(7)]
    gh = FakeGh({head: [pr_node(i, head)] for i, head in enumerate(heads)})
    client = GhGitHubClient(run_gh=gh, batch_size=3)

    data = client.fetch("https://github.com/owner/repo.git", heads)

    assert len(gh.calls) == 3
    batches = [
        sorted(v for k, v in gh.variables(i).items() if k.startswith("head")) for i in range(3)
    ]
    assert batches == [heads[0:3], heads[3:6], heads[6:7]]
    assert {head: [pr.number for pr in prs] for head, prs in data.pull_requests.items()} == {
        head: [i] for i, head in enumerate(heads)
    }


def test_fetch_without_heads_still_gets_default_branch():
    gh = FakeGh(default_branch="main")

    data = GhGitHubClient(run_gh=gh).fetch("https://github.com/owner/repo", [])

    assert data.default_branch == "main"
    assert len(gh.calls) == 1


@pytest.mark.parametrize(
    ("url", "host", "owner", "name"),
    [
        ("git@github.com:owner/repo.git", "github.com", "owner", "repo"),
        ("https://github.com/owner/repo.git", "github.com", "owner", "repo"),
        ("https://github.com/owner/repo", "github.com", "owner", "repo"),
        ("ssh://git@github.com/owner/repo.git", "github.com", "owner", "repo"),
        ("git@ghe.example.com:org/project.git", "ghe.example.com", "org", "project"),
    ],
)
def test_fetch_targets_repository_from_remote_url(url, host, owner, name):
    gh = FakeGh()

    GhGitHubClient(run_gh=gh).fetch(url, ["feat"])

    args = gh.calls[0]
    assert args[args.index("--hostname") + 1] == host
    assert gh.variables(0)["owner"] == owner
    assert gh.variables(0)["name"] == name


def test_fetch_rejects_remote_that_is_not_a_github_repository():
    with pytest.raises(GitHubError):
        GhGitHubClient(run_gh=FakeGh()).fetch("/some/local/path.git", ["feat"])


def test_gh_failure_raises_github_error():
    def failing_gh(args):
        raise GitHubError("gh: authentication required")

    with pytest.raises(GitHubError, match="authentication"):
        GhGitHubClient(run_gh=failing_gh).fetch("git@github.com:owner/repo.git", ["feat"])


def test_repository_without_default_branch_raises_github_error():
    gh = FakeGh(default_branch=None)

    with pytest.raises(GitHubError, match="default branch"):
        GhGitHubClient(run_gh=gh).fetch("git@github.com:owner/repo.git", ["feat"])


def test_graphql_errors_raise_github_error():
    def gh(args):
        return json.dumps({"data": {"repository": None}, "errors": [{"message": "Not found"}]})

    with pytest.raises(GitHubError, match="Not found"):
        GhGitHubClient(run_gh=gh).fetch("git@github.com:owner/repo.git", ["feat"])


def test_missing_gh_raises_github_error(monkeypatch):
    monkeypatch.setenv("PATH", "")

    with pytest.raises(GitHubError, match="not installed"):
        run_gh(["api", "graphql"])
