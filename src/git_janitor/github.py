from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import itertools
import json
import re
import subprocess
from typing import Protocol
from urllib.parse import urlparse

from git_janitor.model import PullRequest, PullRequestState


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GitHubData:
    name_with_owner: str
    default_branch: str
    pull_requests: dict[str, list[PullRequest]] = field(default_factory=dict)
    """Pull requests keyed by head branch name."""
    pull_requests_by_commit: dict[str, list[PullRequest]] = field(default_factory=dict)
    """Pull requests that GitHub associates with each commit."""


class GitHubClient(Protocol):
    def fetch(
        self, remote_url: str, head_names: Sequence[str], commits: Sequence[str] = ()
    ) -> GitHubData: ...


PULL_REQUESTS_PER_HEAD = 20

PULL_REQUEST_FIELDS = "number url state baseRefName headRefName headRefOid"


def run_gh(args: Sequence[str]) -> str:
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise GitHubError("GitHub CLI ('gh') is not installed.") from exc
    if result.returncode != 0:
        raise GitHubError(f"gh {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


class GhGitHubClient:
    """Queries GitHub's GraphQL API through the `gh` CLI, which handles authentication."""

    def __init__(
        self, run_gh: Callable[[Sequence[str]], str] = run_gh, batch_size: int = 50
    ) -> None:
        self.run_gh = run_gh
        self.batch_size = batch_size

    def fetch(
        self, remote_url: str, head_names: Sequence[str], commits: Sequence[str] = ()
    ) -> GitHubData:
        host, owner, name = parse_remote_url(remote_url)
        items = [("head", head) for head in head_names] + [("commit", sha) for sha in commits]
        batches = list(itertools.batched(items, self.batch_size)) or [()]
        by_head: dict[str, list[PullRequest]] = {}
        by_commit: dict[str, list[PullRequest]] = {}
        for batch in batches:
            repository = self._query(host, owner, name, batch)
            try:
                for i, (kind, value) in enumerate(batch):
                    if kind == "head":
                        by_head[value] = _pull_requests(repository[f"pr{i}"]["nodes"])
                    else:
                        commit = repository[f"c{i}"]
                        nodes = commit["associatedPullRequests"]["nodes"] if commit else []
                        by_commit[value] = _pull_requests(nodes)
                name_with_owner = repository["nameWithOwner"]
                default_branch_ref = repository["defaultBranchRef"]
            except (KeyError, TypeError, ValueError) as exc:
                raise GitHubError(f"Unexpected response from GitHub: {exc!r}") from exc
        if not default_branch_ref:
            raise GitHubError(f"{name_with_owner} has no default branch.")
        return GitHubData(
            name_with_owner=name_with_owner,
            default_branch=default_branch_ref["name"],
            pull_requests=by_head,
            pull_requests_by_commit=by_commit,
        )

    def _query(self, host: str, owner: str, name: str, items: Sequence[tuple[str, str]]) -> dict:
        pull_requests = (
            f"first: {PULL_REQUESTS_PER_HEAD}, orderBy: {{field: CREATED_AT, direction: DESC}}"
        )
        nodes = f"{{ nodes {{ {PULL_REQUEST_FIELDS} }} }}"
        variables = ""
        aliases = ""
        for i, (kind, _) in enumerate(items):
            if kind == "head":
                variables += f", $head{i}: String!"
                aliases += f"pr{i}: pullRequests(headRefName: $head{i}, {pull_requests}) {nodes} "
            else:
                variables += f", $commit{i}: GitObjectID!"
                aliases += (
                    f"c{i}: object(oid: $commit{i}) {{ ... on Commit {{ "
                    f"associatedPullRequests({pull_requests}) {nodes} }} }} "
                )
        query = (
            f"query($owner: String!, $name: String!{variables}) {{ "
            f"repository(owner: $owner, name: $name) {{ "
            f"nameWithOwner defaultBranchRef {{ name }} {aliases}}} }}"
        )
        args = ["api", "graphql", "--hostname", host, "-f", f"query={query}"]
        args += ["-f", f"owner={owner}", "-f", f"name={name}"]
        for i, (kind, value) in enumerate(items):
            args += ["-f", f"{kind}{i}={value}"]
        try:
            response = json.loads(self.run_gh(args))
        except json.JSONDecodeError as exc:
            raise GitHubError(f"Unexpected response from GitHub: {exc}") from exc
        if response.get("errors"):
            raise GitHubError(f"GitHub API error: {response['errors']}")
        repository = response.get("data", {}).get("repository")
        if repository is None:
            raise GitHubError(f"Repository {owner}/{name} not found on {host}.")
        return repository


SCP_LIKE_URL = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>[^/].*)$")


def parse_remote_url(url: str) -> tuple[str, str, str]:
    """Parse a GitHub remote URL into (host, owner, repository name)."""
    if "://" in url:
        parsed = urlparse(url)
        host, path = parsed.hostname, parsed.path
    elif match := SCP_LIKE_URL.match(url):
        host, path = match["host"], match["path"]
    else:
        host, path = None, ""
    parts = path.strip("/").removesuffix(".git").split("/")
    if not host or len(parts) != 2 or not all(parts):
        raise GitHubError(f"Remote URL is not a GitHub repository: {url}")
    return host, parts[0], parts[1]


def _pull_requests(nodes: list[dict]) -> list[PullRequest]:
    return [
        PullRequest(
            number=node["number"],
            url=node["url"],
            state=PullRequestState(node["state"]),
            base=node["baseRefName"],
            head_name=node["headRefName"],
            head_oid=node["headRefOid"],
        )
        for node in nodes
    ]
