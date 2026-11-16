"""Where available versions come from. Swap in your own source for private indexes or tests."""

from __future__ import annotations

import re
from typing import Protocol

from packaging.version import InvalidVersion, Version

from ..core.proc import quote, run


def parse_version(v: str) -> Version | None:
    try:
        return Version(v)
    except InvalidVersion:
        return None


class VersionSource(Protocol):
    def available(self, package: str) -> list[str]: ...


class PipIndexSource:
    """Uses ``pip index versions`` against whatever index pip is configured for."""

    def __init__(self, python: str):
        self.python = python

    def available(self, package: str) -> list[str]:
        res = run(f"{quote(self.python)} -m pip index versions {quote(package)} --pre", timeout=120)
        m = re.search(r"Available versions:\s*(.+)", res.output)
        if not res.ok or not m:
            raise RuntimeError(res.tail(500).strip() or "pip index versions failed")
        return [v.strip() for v in m.group(1).split(",") if v.strip()]


class StaticSource:
    def __init__(self, table: dict[str, list[str]]):
        self.table = {k.lower(): v for k, v in table.items()}

    def available(self, package: str) -> list[str]:
        return list(self.table.get(package.lower(), []))
