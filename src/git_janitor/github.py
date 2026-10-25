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


class GitHubClient(Protocol):
    def fetch(self, remote_url: str, head_names: Sequence[str]) -> GitHubData: ...


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

    def fetch(self, remote_url: str, head_names: Sequence[str]) -> GitHubData:
        host, owner, name = parse_remote_url(remote_url)
        batches = list(itertools.batched(head_names, self.batch_size)) or [()]
        pull_requests: dict[str, list[PullRequest]] = {}
        for batch in batches:
            repository = self._query(host, owner, name, batch)
            try:
                for i, head in enumerate(batch):
                    pull_requests[head] = [
                        PullRequest(
                            number=node["number"],
                            url=node["url"],
                            state=PullRequestState(node["state"]),
                            base=node["baseRefName"],
                            head_name=node["headRefName"],
                            head_oid=node["headRefOid"],
                        )
                        for node in repository[f"pr{i}"]["nodes"]
                    ]
                name_with_owner = repository["nameWithOwner"]
                default_branch_ref = repository["defaultBranchRef"]
            except (KeyError, TypeError, ValueError) as exc:
                raise GitHubError(f"Unexpected response from GitHub: {exc!r}") from exc
        if not default_branch_ref:
            raise GitHubError(f"{name_with_owner} has no default branch.")
        return GitHubData(
            name_with_owner=name_with_owner,
            default_branch=default_branch_ref["name"],
            pull_requests=pull_requests,
        )

    def _query(self, host: str, owner: str, name: str, heads: Sequence[str]) -> dict:
        variables = "".join(f", $head{i}: String!" for i in range(len(heads)))
        aliases = "".join(
            f"pr{i}: pullRequests(headRefName: $head{i}, first: {PULL_REQUESTS_PER_HEAD}, "
            "orderBy: {field: CREATED_AT, direction: DESC}) "
            f"{{ nodes {{ {PULL_REQUEST_FIELDS} }} }} "
            for i in range(len(heads))
        )
        query = (
            f"query($owner: String!, $name: String!{variables}) {{ "
            f"repository(owner: $owner, name: $name) {{ "
            f"nameWithOwner defaultBranchRef {{ name }} {aliases}}} }}"
        )
        args = ["api", "graphql", "--hostname", host, "-f", f"query={query}"]
        args += ["-f", f"owner={owner}", "-f", f"name={name}"]
        for i, head in enumerate(heads):
            args += ["-f", f"head{i}={head}"]
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
