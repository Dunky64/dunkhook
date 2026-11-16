"""Dependency-upgrade bot: bump one pin, run the suite, keep or revert.

For each ``name==version`` pin in a requirements file:

1. Try the newest available version.
2. If the suite fails, Claude reads the failure and decides what to try next:
   an older candidate (e.g. the last release before a breaking major), or give up.
   It also classifies the failure (breaking API, transitive conflict, flaky, ...)
   so the report says *why* a bump was rejected.
3. Anything that never goes green is reverted (file and environment).
4. Green bumps are committed one per package on the current branch (``--commit``).

The baseline suite must pass before anything is bumped; otherwise failures can't
be attributed to an upgrade.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..core.gitutil import Git
from ..core.llm import LLM, LLMError
from ..core.proc import quote, run
from ..core.schema import enum, nullable, obj, string
from .versions import PipIndexSource, VersionSource, parse_version

PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?P<extras>\[[^\]]*\])?==(?P<ver>[^\s;#]+)(?P<rest>.*)$")

ADVISOR_SYSTEM = """You advise a dependency-upgrade bot. A version bump made the test-suite fail. \
Read the failure, decide whether it is caused by the upgrade, and pick the best next version to try \
from the candidate list (or null to give up and keep the current pin). Prefer the newest candidate that \
plausibly avoids the failure, e.g. the last release of the previous major when an API was removed. \
Never pick a version that is not in the list."""

ADVICE_SCHEMA = obj(
    category=enum("breaking_api", "dependency_conflict", "environment", "flaky_test", "unrelated", "unknown"),
    diagnosis=string("One or two sentences on what broke and why"),
    next_version=nullable(string("A version from the candidate list, or null")),
    migration_hint=string("What code change would let the project take the newest version; empty if unknown"),
)


@dataclass
class Attempt:
    version: str
    passed: bool
    log_tail: str = ""
    advice: dict | None = None


@dataclass
class BumpResult:
    package: str
    current: str
    final: str
    status: str  # upgraded | reverted | up_to_date | skipped
    attempts: list[Attempt] = field(default_factory=list)
    commit: str | None = None


@dataclass
class Pin:
    line_no: int
    name: str
    version: str
    extras: str
    rest: str

    def render(self, version: str) -> str:
        return f"{self.name}{self.extras}=={version}{self.rest}"


def read_pins(text: str) -> list[Pin]:
    pins = []
    for i, line in enumerate(text.splitlines()):
        m = PIN_RE.match(line.strip())
        if m:
            pins.append(Pin(i, m["name"], m["ver"], m["extras"] or "", m["rest"]))
    return pins


def set_pin(text: str, pin: Pin, version: str) -> str:
    lines = text.splitlines(keepends=True)
    nl = "\n" if lines[pin.line_no].endswith("\n") else ""
    lines[pin.line_no] = pin.render(version) + nl
    return "".join(lines)


class DepBot:
    def __init__(
        self,
        repo: str | Path,
        test_cmd: str,
        requirements: str = "requirements.txt",
        python: str = sys.executable,
        install_cmd: str | None = None,
        llm: LLM | None = None,
        versions: VersionSource | None = None,
        max_attempts: int = 3,
        commit: bool = False,
        include_prereleases: bool = False,
        timeout: float = 1800,
        log=print,
    ):
        self.repo = Path(repo).resolve()
        self.req_path = self.repo / requirements
        self.test_cmd = test_cmd
        self.python = python
        self.install_cmd = install_cmd or "{python} -m pip install -q -r {req}"
        self.llm = llm
        self.versions = versions or PipIndexSource(python)
        self.max_attempts = max_attempts
        self.commit = commit
        self.include_prereleases = include_prereleases
        self.timeout = timeout
        self.log = log
        self.git = Git(self.repo) if commit else None

    # -- environment -------------------------------------------------------
    def _install(self) -> tuple[bool, str]:
        cmd = self.install_cmd.format(python=quote(self.python), req=quote(self.req_path))
        res = run(cmd, cwd=self.repo, timeout=self.timeout)
        return res.ok, res.tail()

    def _test(self) -> tuple[bool, str]:
        res = run(self.test_cmd, cwd=self.repo, timeout=self.timeout)
        return res.ok, res.tail()

    def _try(self, text: str) -> tuple[bool, str]:
        self.req_path.write_text(text)
        ok, out = self._install()
        if not ok:
            return False, "INSTALL FAILED:\n" + out
        return self._test()

    # -- decision making -----------------------------------------------------
    def _candidates(self, pin: Pin) -> list[str]:
        current = parse_version(pin.version)
        newer = []
        for v in self.versions.available(pin.name):
            pv = parse_version(v)
            if pv is None or current is None or pv <= current:
                continue
            if pv.is_prerelease and not self.include_prereleases:
                continue
            newer.append((pv, v))
        return [v for _, v in sorted(newer, reverse=True)]

    def _next(self, pin: Pin, tried: list[str], remaining: list[str], log_tail: str) -> tuple[str | None, dict | None]:
        if not remaining:
            return None, None
        if self.llm is not None:
            try:
                advice = self.llm.json(
                    ADVISOR_SYSTEM,
                    f"Package: {pin.name}\nCurrent pin: {pin.version}\nTried (failed): {', '.join(tried)}\n"
                    f"Candidates still untried (newest first): {', '.join(remaining)}\n\n"
                    f"Failure output (tail):\n```\n{log_tail}\n```",
                    ADVICE_SCHEMA,
                )
            except LLMError as exc:
                self.log(f"  advisor unavailable ({exc}); using heuristic")
            else:
                nxt = advice.get("next_version")
                return (nxt if nxt in remaining else None), advice
        return heuristic_next(tried[-1], remaining), None

    # -- main loop -------------------------------------------------------------
    def run(self, only: list[str] | None = None) -> list[BumpResult]:
        if self.commit and not self.git.is_clean():
            raise RuntimeError("working tree is not clean; commit or stash first (needed for --commit)")
        original = self.req_path.read_text()
        self.log("baseline: installing and running tests")
        ok, out = self._try(original)
        if not ok:
            raise RuntimeError("baseline test run failed; fix the suite before upgrading:\n" + out[-2000:])

        results = []
        for pin in read_pins(original):
            if only and pin.name.lower() not in {o.lower() for o in only}:
                continue
            base = self.req_path.read_text()
            pin = next(p for p in read_pins(base) if p.name == pin.name)
            result = BumpResult(pin.name, pin.version, pin.version, "up_to_date")
            try:
                remaining = self._candidates(pin)
            except Exception as exc:
                result.status = "skipped"
                result.attempts.append(Attempt(pin.version, False, f"could not list versions: {exc}"))
                results.append(result)
                continue
            if not remaining:
                self.log(f"{pin.name}: up to date ({pin.version})")
                results.append(result)
                continue

            version: str | None = remaining[0]
            tried: list[str] = []
            while version and len(tried) < self.max_attempts:
                remaining.remove(version)
                self.log(f"{pin.name}: trying {pin.version} -> {version}")
                ok, out = self._try(set_pin(base, pin, version))
                attempt = Attempt(version, ok, out[-4000:])
                result.attempts.append(attempt)
                tried.append(version)
                if ok:
                    result.status, result.final = "upgraded", version
                    self.log(f"{pin.name}: {version} passes")
                    break
                version, attempt.advice = self._next(pin, tried, remaining, out)
                if attempt.advice:
                    self.log(f"  advisor: [{attempt.advice['category']}] {attempt.advice['diagnosis']}")

            if result.status != "upgraded":
                result.status = "reverted"
                self.log(f"{pin.name}: reverting to {pin.version}")
                self.req_path.write_text(base)
                self._install()
            elif self.commit:
                msg = f"deps: bump {pin.name} from {pin.version} to {result.final}"
                if len(result.attempts) > 1:
                    msg += "\n\nTried and rejected: " + ", ".join(a.version for a in result.attempts if not a.passed)
                result.commit = self.git.commit_all(msg, [str(self.req_path.relative_to(self.repo))])
            results.append(result)
        return results


def heuristic_next(failed: str, remaining: list[str]) -> str | None:
    """Without an advisor: step down to the newest release of an older major/minor."""
    fv = parse_version(failed)
    for v in remaining:
        pv = parse_version(v)
        if pv and fv and (pv.release[:1] < fv.release[:1]):
            return v
    for v in remaining:
        pv = parse_version(v)
        if pv and fv and (pv.release[:2] < fv.release[:2]):
            return v
    return None


def render_report(results: list[BumpResult]) -> str:
    lines = ["# Dependency upgrade report", "", "| Package | From | To | Status | Attempts |", "|---|---|---|---|---|"]
    for r in results:
        tried = ", ".join(f"{a.version} {'✅' if a.passed else '❌'}" for a in r.attempts) or "-"
        lines.append(f"| {r.package} | {r.current} | {r.final} | {r.status} | {tried} |")
    for r in results:
        notes = [a for a in r.attempts if a.advice]
        if not notes:
            continue
        lines += ["", f"## {r.package}", ""]
        for a in notes:
            adv = a.advice
            lines.append(f"- **{a.version}** — {adv['category']}: {adv['diagnosis']}")
            if adv.get("migration_hint"):
                lines.append(f"  - To adopt it: {adv['migration_hint']}")
    return "\n".join(lines) + "\n"
