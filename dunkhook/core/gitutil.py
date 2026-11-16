"""Minimal git wrapper. Raises GitError on failure so callers cannot ignore it."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def run(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True
        )
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    def is_repo(self) -> bool:
        return self.run("rev-parse", "--is-inside-work-tree", check=False).strip() == "true"

    def current_branch(self) -> str:
        return self.run("rev-parse", "--abbrev-ref", "HEAD").strip()

    def is_clean(self) -> bool:
        return self.run("status", "--porcelain").strip() == ""

    def checkout(self, branch: str, create: bool = False) -> None:
        if create:
            self.run("checkout", "-b", branch)
        else:
            self.run("checkout", branch)

    def commit_all(self, message: str, paths: list[str] | None = None) -> str:
        self.run("add", *(paths or ["-A"]))
        self.run("commit", "-m", message)
        return self.run("rev-parse", "HEAD").strip()

    def restore(self, *paths: str) -> None:
        self.run("checkout", "--", *paths)

    def github_slug(self) -> str | None:
        """owner/repo for a GitHub ``origin`` remote, else None."""
        url = self.run("remote", "get-url", "origin", check=False).strip()
        m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url)
        return f"{m.group(1)}/{m.group(2)}" if m else None
