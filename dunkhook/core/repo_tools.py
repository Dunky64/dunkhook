"""Read-only repository tools shared by agents (confined to a root directory)."""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from .llm import Tool
from .schema import integer, obj, string

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build"}


def safe_path(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"path escapes repository root: {rel}")
    return p


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath, name)


def read_tool(root: Path) -> Tool:
    def fn(args: dict) -> str:
        path = safe_path(root, args["path"])
        lines = path.read_text(errors="replace").splitlines()
        start = max(1, args["start_line"] or 1)
        end = min(len(lines), args["end_line"] or len(lines))
        return "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(start, end + 1)) or "(empty)"

    return Tool(
        name="read_file",
        description="Read a file from the repository with line numbers. Use 0 for start_line/end_line to read from the beginning / to the end.",
        input_schema=obj(path=string("Path relative to the repository root"), start_line=integer(), end_line=integer()),
        fn=fn,
    )


def search_tool(root: Path) -> Tool:
    def fn(args: dict) -> str:
        rx = re.compile(args["pattern"])
        glob = args["glob"] or "*"
        hits = []
        for f in iter_files(root):
            rel = f.relative_to(root).as_posix()
            if not fnmatch.fnmatch(rel, glob) and not fnmatch.fnmatch(f.name, glob):
                continue
            try:
                for n, line in enumerate(f.read_text(errors="strict").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{rel}:{n}: {line.strip()[:200]}")
                        if len(hits) >= 200:
                            return "\n".join(hits) + "\n[stopped at 200 matches]"
            except (UnicodeDecodeError, OSError):
                continue
        return "\n".join(hits) or "no matches"

    return Tool(
        name="search_code",
        description="Regex search across repository files. glob filters paths, e.g. '*.py' or 'src/*' ('' for all).",
        input_schema=obj(pattern=string("Python regular expression"), glob=string()),
        fn=fn,
    )


def list_tool(root: Path) -> Tool:
    def fn(args: dict) -> str:
        glob = args["glob"] or "*"
        out = [
            f.relative_to(root).as_posix()
            for f in iter_files(root)
            if fnmatch.fnmatch(f.relative_to(root).as_posix(), glob)
        ]
        return "\n".join(sorted(out)[:500]) or "no files"

    return Tool(
        name="list_files",
        description="List repository files matching a glob such as '*.py' or 'app/**'.",
        input_schema=obj(glob=string()),
        fn=fn,
    )
