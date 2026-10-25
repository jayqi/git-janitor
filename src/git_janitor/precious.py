"""Find ignored files in a worktree that look like personal work rather than generated files."""

from collections.abc import Sequence
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
    "*.swp",
    "*.swo",
    "*~",
)
DISPOSABLE_CONFIG_KEY = "janitor.disposable"


def find_precious_files(
    worktree: Path, ignored: Sequence[str], skip: Sequence[Path] = ()
) -> list[PreciousFile]:
    """Precious files among a worktree's ignored paths, as listed by `git.worktree_status`.

    Directories in `skip` aren't searched.
    """
    if not ignored:
        return []
    disposable = BUILTIN_DISPOSABLE + tuple(
        pattern.rstrip("/") for pattern in git.config_get_all(DISPOSABLE_CONFIG_KEY, worktree)
    )
    sources = git.ignore_sources(ignored, cwd=worktree)
    tracked = git.tracked_files(
        [source for source in set(sources.values()) if not Path(source).is_absolute()],
        cwd=worktree,
    )

    files = []
    for entry, source in sources.items():
        if _is_disposable(Path(entry).name, disposable):
            continue
        paths, contains_repository = _walk_files(worktree / entry, skip)
        if _is_repo_gitignore(source, worktree, tracked) and not contains_repository:
            continue
        for path in paths:
            relative = path.relative_to(worktree)
            if any(_is_disposable(part, disposable) for part in relative.parts):
                continue
            stat = path.lstat()
            files.append(
                PreciousFile(
                    path=relative.as_posix(),
                    size=stat.st_size,
                    modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                )
            )
    return sorted(files, key=lambda f: f.path)


def _is_repo_gitignore(source: str, worktree: Path, tracked: set[str]) -> bool:
    """Whether `source` is a `.gitignore` committed to the repository."""
    return Path(source).name == ".gitignore" and source in tracked


def _is_disposable(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch(name, pattern) for pattern in patterns)


def _walk_files(path: Path, skip: Sequence[Path]) -> tuple[list[Path], bool]:
    """Files under `path`, excluding git's own files, and whether it contains a repository."""
    if path.is_symlink() or not path.is_dir():
        return [path], False
    files = []
    contains_repository = False
    for root, dirs, names in os.walk(path):
        if ".git" in dirs or ".git" in names:
            contains_repository = True
        dirs[:] = [d for d in dirs if d != ".git" and Path(root, d) not in skip]
        files.extend(Path(root, name) for name in names if name != ".git")
    return files, contains_repository
