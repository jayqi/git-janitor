"""Find ignored files in a worktree that look like personal work rather than generated files."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
import os
from pathlib import Path

from git_janitor import git
from git_janitor.model import PreciousEntry

BUILTIN_DISPOSABLE = (
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    ".idea",
    ".vscode",
    ".venv",
    "*~",
)
DISPOSABLE_CONFIG_KEY = "janitor.disposable"


CACHEDIR_TAG_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"
"""Marks a cache directory: https://bford.info/cachedir/"""


@dataclass(frozen=True, slots=True)
class IgnoredScan:
    precious: tuple[PreciousEntry, ...]
    nested_repositories: tuple[str, ...]
    """Roots of git repositories inside precious ignored directories."""
    unreadable: tuple[str, ...]


def scan_ignored(worktree: Path, ignored: Sequence[str]) -> IgnoredScan:
    """Find what would be lost among a worktree's ignored paths, as listed by
    `git.worktree_status`.

    An ignored path is disposable if a committed `.gitignore` matched it, if it's in a cache
    directory, or if its name matches a disposable pattern. Anything else is precious. Git
    repositories inside precious directories are reported, as are paths that can't be read.
    """
    if not ignored:
        return IgnoredScan(precious=(), nested_repositories=(), unreadable=())
    patterns = BUILTIN_DISPOSABLE + tuple(
        pattern.rstrip("/") for pattern in git.config_get_all(DISPOSABLE_CONFIG_KEY, worktree)
    )
    sources = git.ignore_sources(ignored, cwd=worktree)
    tracked = git.tracked_files(
        [source for source in set(sources.values()) if not Path(source).is_absolute()],
        cwd=worktree,
    )
    scan = _Scan(worktree, patterns)
    for entry in ignored:
        source = sources.get(entry)
        if source is None or not _is_committed_gitignore(source, tracked):
            scan.add(entry)
    return IgnoredScan(
        precious=tuple(sorted(scan.entries, key=lambda e: e.path)),
        nested_repositories=tuple(sorted(scan.repositories)),
        unreadable=tuple(sorted(scan.unreadable)),
    )


def _is_committed_gitignore(source: str, tracked: set[str]) -> bool:
    return Path(source).name == ".gitignore" and source in tracked


class _Scan:
    def __init__(self, worktree: Path, patterns: tuple[str, ...]) -> None:
        self.worktree = worktree
        self.patterns = patterns
        self.entries: list[PreciousEntry] = []
        self.repositories: list[str] = []
        self.unreadable: list[str] = []

    def add(self, entry: str) -> None:
        """Scan an ignored entry, recording it if it holds precious files."""
        path = self.worktree / entry
        if self._is_disposable(path) or any(
            _is_cache_directory(parent)
            for parent in path.parents
            if parent.is_relative_to(self.worktree)
        ):
            return
        if path.is_symlink() or not path.is_dir():
            files = [path]
        elif self._enter(path):
            files = self._walk(path)
        else:
            return
        stats = [stat for file in files if (stat := self._lstat(file))]
        if stats:
            self.entries.append(
                PreciousEntry(
                    path=entry,
                    files=len(stats),
                    size=sum(stat.st_size for stat in stats),
                    modified=datetime.fromtimestamp(max(stat.st_mtime for stat in stats), tz=UTC),
                )
            )

    def _walk(self, directory: Path) -> list[Path]:
        def on_error(error: OSError) -> None:
            self.unreadable.append(self._relative(Path(error.filename)))

        files = []
        for root, dirs, names in os.walk(directory, onerror=on_error):
            dirs[:] = [d for d in dirs if self._enter(Path(root, d))]
            files += [Path(root, n) for n in names if not self._is_disposable(Path(root, n))]
        return files

    def _enter(self, directory: Path) -> bool:
        """Whether to search `directory`. Records it if it's a repository."""
        if self._is_disposable(directory) or _is_cache_directory(directory):
            return False
        if os.path.lexists(directory / ".git"):
            self.repositories.append(self._relative(directory))
            return False
        return True

    def _lstat(self, path: Path) -> os.stat_result | None:
        try:
            return path.lstat()
        except OSError:
            self.unreadable.append(self._relative(path))
            return None

    def _is_disposable(self, path: Path) -> bool:
        return any(
            fnmatch(part, pattern)
            for part in path.relative_to(self.worktree).parts
            for pattern in self.patterns
        )

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.worktree).as_posix()


def _is_cache_directory(directory: Path) -> bool:
    try:
        with (directory / "CACHEDIR.TAG").open("rb") as f:
            return f.read(len(CACHEDIR_TAG_SIGNATURE)) == CACHEDIR_TAG_SIGNATURE
    except OSError:
        return False
