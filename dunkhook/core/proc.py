"""Subprocess helpers with timeouts and combined output."""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Result:
    code: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out

    def tail(self, n: int = 8000) -> str:
        return self.output[-n:]


def run(
    cmd: str | list[str],
    cwd: str | Path | None = None,
    timeout: float = 600,
    env: dict | None = None,
) -> Result:
    """Run ``cmd``; strings go through the shell, lists do not."""
    full_env = {**os.environ, **(env or {})}
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            shell=isinstance(cmd, str),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=full_env,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        return Result(code=-1, output=out + f"\n[timed out after {timeout}s]", timed_out=True)
    return Result(code=proc.returncode, output=proc.stdout + proc.stderr)


def quote(s: str | Path) -> str:
    return shlex.quote(str(s))
