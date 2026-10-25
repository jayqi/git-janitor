"""Find ignored files in a worktree that look like personal work rather than generated files."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
import os
from pathlib import Path

from git_janitor import git
from git_janitor.model import PreciousFile

BUILTIN_DISPOSABLE = (
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    ".idea",
    ".vscode",
    "*~",
)
DISPOSABLE_CONFIG_KEY = "janitor.disposable"


CACHEDIR_TAG_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"
"""Marks a cache directory: https://bford.info/cachedir/"""


@dataclass(frozen=True, slots=True)
class IgnoredScan:
    precious_files: tuple[PreciousFile, ...]
    nested_repositories: tuple[str, ...]
    """Roots of git repositories inside ignored directories."""
    unreadable: tuple[str, ...]


def scan_ignored(worktree: Path, ignored: Sequence[str], skip: Sequence[Path] = ()) -> IgnoredScan:
    """Find what would be lost among a worktree's ignored paths, as listed by
    `git.worktree_status`. Directories in `skip` aren't searched.

    An ignored path is disposable if a committed `.gitignore` matched it, if it's in a cache
    directory, or if its name matches a disposable pattern. Anything else is precious. Git
    repositories nested anywhere in ignored paths are reported, as are paths that can't be read.
    """
    if not ignored:
        return IgnoredScan(precious_files=(), nested_repositories=(), unreadable=())
    patterns = BUILTIN_DISPOSABLE + tuple(
        pattern.rstrip("/") for pattern in git.config_get_all(DISPOSABLE_CONFIG_KEY, worktree)
    )
    sources = git.ignore_sources(ignored, cwd=worktree)
    tracked = git.tracked_files(
        [source for source in set(sources.values()) if not Path(source).is_absolute()],
        cwd=worktree,
    )
    scan = _Scan(worktree, patterns, set(skip))
    for entry in ignored:
        source = sources.get(entry)
        committed = source is not None and _is_committed_gitignore(source, tracked)
        scan.add(worktree / entry, list_files=not committed)
    return IgnoredScan(
        precious_files=tuple(sorted(scan.files, key=lambda f: f.path)),
        nested_repositories=tuple(sorted(scan.repositories)),
        unreadable=tuple(sorted(scan.unreadable)),
    )


def _is_committed_gitignore(source: str, tracked: set[str]) -> bool:
    return Path(source).name == ".gitignore" and source in tracked


class _Scan:
    def __init__(self, worktree: Path, patterns: tuple[str, ...], skip: set[Path]) -> None:
        self.worktree = worktree
        self.patterns = patterns
        self.skip = skip
        self.files: list[PreciousFile] = []
        self.repositories: list[str] = []
        self.unreadable: list[str] = []

    def add(self, path: Path, *, list_files: bool) -> None:
        """Scan an ignored path, listing its files as precious if `list_files`.

        Disposable-named files aren't listed, but disposable-named directories are still
        searched for repositories. Cache directories aren't searched at all.
        """
        if any(
            _is_cache_directory(parent)
            for parent in path.parents
            if parent.is_relative_to(self.worktree)
        ):
            return
        if path.is_symlink() or not path.is_dir():
            if list_files and not self._is_disposable(path):
                self._add_file(path)
            return
        if not self._enter(path):
            return

        def on_error(error: OSError) -> None:
            self.unreadable.append(self._relative(Path(error.filename)))

        for root, dirs, names in os.walk(path, onerror=on_error):
            dirs[:] = [d for d in dirs if self._enter(Path(root, d))]
            if list_files:
                for name in names:
                    if not self._is_disposable(Path(root, name)):
                        self._add_file(Path(root, name))

    def _enter(self, directory: Path) -> bool:
        """Whether to search `directory`. Records it if it's a repository."""
        if directory in self.skip:
            return False
        if _is_cache_directory(directory):
            return False
        if os.path.lexists(directory / ".git"):
            self.repositories.append(self._relative(directory))
            return False
        return True

    def _add_file(self, path: Path) -> None:
        try:
            stat = path.lstat()
        except OSError:
            self.unreadable.append(self._relative(path))
            return
        self.files.append(
            PreciousFile(
                path=self._relative(path),
                size=stat.st_size,
                modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            )
        )

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
