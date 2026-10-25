from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
import subprocess

import pytest

from git_janitor.github import GitHubData
from git_janitor.model import PullRequest, PullRequestState


@dataclass
class FakeGitHub:
    default_branch: str = "main"
    name_with_owner: str = "owner/repo"
    pull_requests: list[PullRequest] = field(default_factory=list)

    def fetch(
        self, remote_url: str, head_names: Sequence[str], commits: Sequence[str] = ()
    ) -> GitHubData:
        by_head = {
            name: [pr for pr in self.pull_requests if pr.head_name == name] for name in head_names
        }
        by_commit = {
            sha: [pr for pr in self.pull_requests if pr.head_oid == sha] for sha in commits
        }
        return GitHubData(
            name_with_owner=self.name_with_owner,
            default_branch=self.default_branch,
            pull_requests=by_head,
            pull_requests_by_commit=by_commit,
        )

    def get(self, number: int) -> PullRequest:
        return next(pr for pr in self.pull_requests if pr.number == number)

    def update(self, number: int, **changes) -> None:
        self.pull_requests = [
            replace(pr, **changes) if pr.number == number else pr for pr in self.pull_requests
        ]


def run_git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


class Sandbox:
    """A clone of a bare `origin` repo, plus a fake GitHub that knows about its pull requests."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.origin = root / "origin.git"
        self.repo = root / "repo"
        self.github = FakeGitHub()
        self._commit_counter = 0

        run_git("init", "--bare", "--initial-branch=main", str(self.origin), cwd=root)
        run_git("clone", str(self.origin), str(self.repo), cwd=root)
        run_git("checkout", "-b", "main", cwd=self.repo)
        self.commit()
        run_git("push", "-u", "origin", "main", cwd=self.repo)
        run_git("remote", "set-head", "origin", "main", cwd=self.repo)

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return run_git(*args, cwd=cwd or self.repo)

    def tip(self, ref: str) -> str:
        return self.git("rev-parse", ref)

    def commit(self, cwd: Path | None = None, message: str | None = None) -> str:
        """Add a new file and commit it on whatever is checked out in `cwd`."""
        cwd = cwd or self.repo
        self._commit_counter += 1
        name = f"file{self._commit_counter}.txt"
        (cwd / name).write_text(f"content {self._commit_counter}\n")
        self.git("add", name, cwd=cwd)
        self.git("commit", "-m", message or f"Commit {self._commit_counter}", cwd=cwd)
        return self.git("rev-parse", "HEAD", cwd=cwd)

    def new_branch(self, name: str, commits: int = 1, start: str = "main") -> str:
        """Create a branch with `commits` new commits without leaving the current checkout."""
        current = self.git("branch", "--show-current")
        self.git("checkout", "-q", "-b", name, start)
        for _ in range(commits):
            self.commit()
        self.git("checkout", "-q", current)
        return self.tip(name)

    def add_commits(self, branch: str, commits: int = 1) -> list[str]:
        """Add commits to `branch` and return their SHAs, oldest first."""
        current = self.git("branch", "--show-current")
        self.git("checkout", "-q", branch)
        shas = [self.commit() for _ in range(commits)]
        self.git("checkout", "-q", current)
        return shas

    def add_worktree(self, branch: str, *, create: bool = False) -> Path:
        path = self.root / f"wt-{branch.replace('/', '-')}"
        if create:
            self.git("worktree", "add", "-q", "-b", branch, str(path), "main")
        else:
            self.git("worktree", "add", "-q", str(path), branch)
        return path

    def add_detached_worktree(self, name: str, commit: str = "main") -> Path:
        path = self.root / f"wt-{name}"
        self.git("worktree", "add", "-q", "--detach", str(path), commit)
        return path

    def push(self, branch: str) -> None:
        self.git("push", "-q", "-u", "origin", branch)

    def open_pr(self, branch: str, base: str = "main") -> int:
        """Push `branch` and open a pull request for it, with `refs/pull/<N>/head` on origin."""
        self.push(branch)
        number = len(self.github.pull_requests) + 1
        head_oid = self.tip(branch)
        self.git("update-ref", f"refs/pull/{number}/head", head_oid, cwd=self.origin)
        self.github.pull_requests.append(
            PullRequest(
                number=number,
                url=f"https://github.com/owner/repo/pull/{number}",
                state=PullRequestState.OPEN,
                base=base,
                head_name=branch,
                head_oid=head_oid,
            )
        )
        return number

    def push_to_pr(self, number: int, branch: str) -> None:
        """Push new local commits on `branch` to its open pull request."""
        self.push(branch)
        head_oid = self.tip(branch)
        self.git("update-ref", f"refs/pull/{number}/head", head_oid, cwd=self.origin)
        self.github.update(number, head_oid=head_oid)

    def push_to_pr_from_elsewhere(self, number: int) -> str:
        """Add a commit to a pull request from another clone, so the local repo lacks it."""
        pr = self.github.get(number)
        elsewhere = self.root / f"elsewhere-{number}"
        run_git(
            "clone",
            "-q",
            "--branch",
            pr.head_name,
            str(self.origin),
            str(elsewhere),
            cwd=self.root,
        )
        head_oid = self.commit(cwd=elsewhere)
        run_git("push", "-q", "origin", pr.head_name, cwd=elsewhere)
        self.git("update-ref", f"refs/pull/{number}/head", head_oid, cwd=self.origin)
        self.github.update(number, head_oid=head_oid)
        return head_oid

    def squash_merge(self, number: int, *, delete_head: bool = True) -> str:
        """Squash-merge a pull request on origin, the way GitHub does."""
        pr = self.github.get(number)
        base_ref = f"refs/heads/{pr.base}"
        tree = run_git(
            "merge-tree", "--write-tree", base_ref, pr.head_oid, cwd=self.origin
        ).splitlines()[0]
        squash = run_git(
            "commit-tree", tree, "-p", base_ref, "-m", f"Squash (#{number})", cwd=self.origin
        )
        run_git("update-ref", base_ref, squash, cwd=self.origin)
        if delete_head:
            run_git("update-ref", "-d", f"refs/heads/{pr.head_name}", cwd=self.origin)
        self.github.update(number, state=PullRequestState.MERGED)
        return squash

    def merge_commit_merge(self, number: int) -> str:
        """Merge a pull request on origin with a merge commit, the way GitHub does."""
        pr = self.github.get(number)
        base_ref = f"refs/heads/{pr.base}"
        tree = run_git(
            "merge-tree", "--write-tree", base_ref, pr.head_oid, cwd=self.origin
        ).splitlines()[0]
        merge = run_git(
            "commit-tree",
            tree,
            "-p",
            base_ref,
            "-p",
            pr.head_oid,
            "-m",
            f"Merge #{number}",
            cwd=self.origin,
        )
        run_git("update-ref", base_ref, merge, cwd=self.origin)
        run_git("update-ref", "-d", f"refs/heads/{pr.head_name}", cwd=self.origin)
        self.github.update(number, state=PullRequestState.MERGED)
        return merge

    def close_pr(self, number: int) -> None:
        self.github.update(number, state=PullRequestState.CLOSED)

    def push_to_origin_main(self, commit: str) -> None:
        run_git("update-ref", "refs/heads/main", commit, cwd=self.origin)


@pytest.fixture(autouse=True)
def isolated_git_config(tmp_path_factory: pytest.TempPathFactory, monkeypatch) -> Path:
    """Keep the developer's own git config and global excludes out of tests."""
    home = tmp_path_factory.mktemp("home")
    global_config = home / ".gitconfig"
    global_excludes = home / "global-excludes"
    global_excludes.write_text("")
    global_config.write_text(
        "[user]\n"
        "\tname = Test\n"
        "\temail = test@example.com\n"
        "[core]\n"
        f"\texcludesFile = {global_excludes}\n"
        "[init]\n"
        "\tdefaultBranch = main\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return global_excludes


@pytest.fixture
def global_excludes(isolated_git_config: Path) -> Path:
    return isolated_git_config


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path.resolve())


@pytest.fixture
def unremovable_worktree(sandbox):
    """A merged branch in a worktree that `git worktree remove` can't delete."""
    path = sandbox.add_worktree("feat", create=True)
    (path / "readonly").mkdir()
    (path / "readonly" / "file.txt").write_text("content\n")
    sandbox.git("add", "readonly", cwd=path)
    sandbox.git("commit", "-m", "Add read-only directory", cwd=path)
    sandbox.squash_merge(sandbox.open_pr("feat"))
    (path / "readonly").chmod(0o555)
    yield path
    (path / "readonly").chmod(0o755)


@pytest.fixture
def restore_permissions():
    paths = []
    yield paths
    for path in paths:
        path.chmod(0o755)
