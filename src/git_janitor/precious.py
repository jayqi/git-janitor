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


def find_precious_files(worktree: Path, ignored: Sequence[str]) -> list[PreciousFile]:
    """Precious files among a worktree's ignored paths, as listed by `git.worktree_status`."""
    if not ignored:
        return []
    disposable = BUILTIN_DISPOSABLE + tuple(
        pattern.rstrip("/") for pattern in git.config_get_all(DISPOSABLE_CONFIG_KEY, worktree)
    )

    precious_entries = [
        entry
        for entry, source in git.ignore_sources(ignored, cwd=worktree).items()
        if not _is_repo_gitignore(source, worktree)
        and not _is_disposable(Path(entry).name, disposable)
    ]

    files = []
    for entry in precious_entries:
        for path in _walk_files(worktree / entry):
            relative = path.relative_to(worktree)
            if any(_is_disposable(part, disposable) for part in relative.parts):
                continue
            stat = path.stat()
            files.append(
                PreciousFile(
                    path=relative.as_posix(),
                    size=stat.st_size,
                    modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                )
            )
    return sorted(files, key=lambda f: f.path)


def _is_repo_gitignore(source: str, worktree: Path) -> bool:
    source_path = (worktree / source).resolve()
    return source_path.name == ".gitignore" and source_path.is_relative_to(worktree.resolve())


def _is_disposable(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch(name, pattern) for pattern in patterns)


def _walk_files(path: Path) -> list[Path]:
    if not path.is_dir():
        return [path]
    return [Path(root) / name for root, _, names in os.walk(path) for name in names]
