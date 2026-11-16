"""Red-team harness: a builder writes code, a breaker attacks it, survivors are kept.

Round loop
----------
1. The breaker writes a new pytest file aimed at the current ``solution.py``.
   Files that fail to collect are rejected and the breaker is told why.
2. If every attack passes, the solution survived the round.
3. Otherwise the builder sees the failures and returns a fix. It may *dispute*
   tests it believes contradict the spec; a separate judge rules on each dispute
   and invalid tests are deselected for good.
4. The fix must pass the *whole* accepted suite (all earlier rounds too). If the
   builder can't get there in ``fix_attempts`` tries, the code rolls back to the
   last survivor and the unbeaten tests are filed under ``open_breaks/``.

The output directory holds the surviving ``solution.py``, the accepted test
suite, any open breaks, and ``report.md``.

Model-written code runs on this machine. Run the harness inside a container or
VM if that matters to you.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..core.llm import LLM
from ..core.proc import run
from ..core.schema import array, enum, obj, string

BUILDER_SYSTEM = """You are the BUILDER in a red-team exercise. You write a single Python module, \
solution.py, implementing the spec exactly. Adversarial tests will be written against your code, so \
handle edge cases the spec implies: empty inputs, boundaries, invalid types where the spec defines \
behaviour, unicode, large inputs. Use only the standard library. Return the complete file every time."""

BREAKER_SYSTEM = """You are the BREAKER in a red-team exercise. Your job is to find inputs where \
solution.py violates the spec. Write ONE pytest file that imports from `solution`. Every assertion must \
follow from the spec text: never invent requirements the spec does not state. Prefer a few sharp, \
well-named tests over many redundant ones. Do not repeat attacks that already exist. Tests must be \
deterministic and finish within a few seconds."""

JUDGE_SYSTEM = """You are an impartial JUDGE. A builder disputes a test written by a breaker. Decide \
only from the spec: a test is VALID if the spec requires the behaviour it asserts, INVALID if it asserts \
something the spec does not require or contradicts. Ambiguity favours the builder."""

BUILD_SCHEMA = obj(
    code=string("Complete contents of solution.py"),
    disputes=array(
        obj(test=string("Test function name, e.g. test_empty_input"), reason=string()),
        "Tests you believe contradict or go beyond the spec. Leave empty if none.",
    ),
    notes=string("What you changed and why, briefly"),
)
BREAK_SCHEMA = obj(test_code=string("Complete pytest file"), attack_summary=string())
JUDGE_SCHEMA = obj(verdict=enum("valid", "invalid"), reasoning=string())

FAILED_RE = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?: - |$)", re.M)


@dataclass
class RoundLog:
    number: int
    attack_summary: str
    outcome: str  # survived | patched | rejected_attack | open_break
    failures: list[str] = field(default_factory=list)
    disputes: list[dict] = field(default_factory=list)
    notes: str = ""


@dataclass
class RedTeamResult:
    out_dir: Path
    rounds: list[RoundLog]
    accepted_tests: int
    open_breaks: int

    @property
    def survived_all(self) -> bool:
        return self.open_breaks == 0


class Workspace:
    """Scratch directory holding the candidate solution and test files."""

    def __init__(self, root: Path, timeout: float):
        self.root = root
        self.tests = root / "tests"
        self.tests.mkdir(parents=True, exist_ok=True)
        (self.tests / "conftest.py").write_text(
            "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))\n"
        )
        self.timeout = timeout

    def write_solution(self, code: str) -> None:
        (self.root / "solution.py").write_text(code)

    def solution(self) -> str:
        return (self.root / "solution.py").read_text()

    def pytest(self, targets: list[str], deselect: set[str]) -> tuple[int, str, list[str]]:
        cmd = [sys.executable, "-m", "pytest", "-q", "-rfE", "--tb=short", "-p", "no:cacheprovider", *targets]
        for node in sorted(deselect):
            cmd += ["--deselect", node]
        res = run(cmd, cwd=self.root, timeout=self.timeout, env={"PYTHONDONTWRITEBYTECODE": "1"})
        code = 1 if res.timed_out else res.code
        return code, res.output, FAILED_RE.findall(res.output)


def run_redteam(
    spec: str,
    llm: LLM,
    out_dir: str | Path,
    rounds: int = 5,
    fix_attempts: int = 3,
    initial_code: str | None = None,
    timeout: float = 60,
    breaker_llm: LLM | None = None,
    judge_llm: LLM | None = None,
    log=print,
) -> RedTeamResult:
    breaker_llm = breaker_llm or llm
    judge_llm = judge_llm or llm
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ws = Workspace(Path(tempfile.mkdtemp(prefix="redteam-")), timeout)

    if initial_code is None:
        built = llm.json(BUILDER_SYSTEM, f"SPEC:\n{spec}\n\nWrite solution.py.", BUILD_SCHEMA)
        initial_code = built["code"]
    ws.write_solution(initial_code)
    survivor = initial_code

    accepted: list[str] = []  # accepted test file names (relative to tests/)
    deselect: set[str] = set()  # node ids ruled invalid by the judge
    open_breaks: list[tuple[str, list[str]]] = []
    history: list[RoundLog] = []
    breaker_feedback = ""

    for n in range(1, rounds + 1):
        log(f"[round {n}] breaker attacking")
        existing = "\n\n".join(f"# {name}\n{(ws.tests / name).read_text()}" for name in accepted) or "(none yet)"
        attack = breaker_llm.json(
            BREAKER_SYSTEM,
            f"SPEC:\n{spec}\n\nCURRENT solution.py:\n```python\n{ws.solution()}\n```\n\n"
            f"EXISTING ACCEPTED TESTS:\n{existing}\n\n{breaker_feedback}\nWrite your new attack file.",
            BREAK_SCHEMA,
        )
        breaker_feedback = ""
        test_name = f"test_round_{n}.py"
        test_path = ws.tests / test_name
        test_path.write_text(attack["test_code"])
        code, output, failed = ws.pytest([f"tests/{test_name}"], deselect)

        if code not in (0, 1) or (code == 1 and not failed):
            # 2: interrupted/collection error, 4: usage error, 5: no tests collected
            test_path.unlink()
            breaker_feedback = f"Your previous attack was rejected because it did not run cleanly:\n{output[-3000:]}\n"
            history.append(RoundLog(n, attack["attack_summary"], "rejected_attack", notes=output[-500:]))
            log(f"[round {n}] attack rejected (pytest exit {code})")
            continue

        if code == 0:
            accepted.append(test_name)
            history.append(RoundLog(n, attack["attack_summary"], "survived"))
            log(f"[round {n}] solution survived")
            continue

        log(f"[round {n}] {len(failed)} break(s): {', '.join(failed)}")
        round_log = RoundLog(n, attack["attack_summary"], "patched", failures=list(failed))
        accepted.append(test_name)
        suite = [f"tests/{t}" for t in accepted]
        failure_text = output
        fixed = False
        for attempt in range(1, fix_attempts + 1):
            fix = llm.json(
                BUILDER_SYSTEM,
                f"SPEC:\n{spec}\n\nCURRENT solution.py:\n```python\n{ws.solution()}\n```\n\n"
                f"These tests fail:\n```\n{failure_text[-6000:]}\n```\n\n"
                f"Test file {test_name}:\n```python\n{test_path.read_text()}\n```\n\n"
                "Fix solution.py. If a test asserts behaviour the spec does not require, dispute it instead of "
                "bending the implementation.",
                BUILD_SCHEMA,
            )
            ws.write_solution(fix["code"])
            round_log.notes = fix["notes"]
            for d in fix["disputes"]:
                verdict = judge_llm.json(
                    JUDGE_SYSTEM,
                    f"SPEC:\n{spec}\n\nTEST FILE:\n```python\n{test_path.read_text()}\n```\n\n"
                    f"Disputed test: {d['test']}\nBuilder's reason: {d['reason']}",
                    JUDGE_SCHEMA,
                )
                round_log.disputes.append({**d, **verdict})
                if verdict["verdict"] == "invalid":
                    deselect.add(f"tests/{test_name}::{d['test']}")
                    log(f"[round {n}] judge struck {d['test']}")
            code, failure_text, failed = ws.pytest(suite, deselect)
            if code == 0:
                fixed = True
                survivor = ws.solution()
                log(f"[round {n}] patched on attempt {attempt}")
                break
        if not fixed:
            ws.write_solution(survivor)
            accepted.remove(test_name)
            open_breaks.append((test_name, failed))
            round_log.outcome = "open_break"
            log(f"[round {n}] builder could not fix; rolled back to last survivor")
        history.append(round_log)

    # Persist what survived.
    (out / "solution.py").write_text(survivor)
    tests_out = out / "tests"
    if tests_out.exists():
        shutil.rmtree(tests_out)
    tests_out.mkdir()
    (tests_out / "conftest.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))\n"
    )
    for name in accepted:
        shutil.copy(ws.tests / name, tests_out / name)
    if deselect:
        (out / "struck_tests.json").write_text(json.dumps(sorted(deselect), indent=2))
    breaks_dir = out / "open_breaks"
    if breaks_dir.exists():
        shutil.rmtree(breaks_dir)
    if open_breaks:
        breaks_dir.mkdir()
        for name, _ in open_breaks:
            shutil.copy(ws.tests / name, breaks_dir / name)
    result = RedTeamResult(out, history, len(accepted), len(open_breaks))
    (out / "report.md").write_text(render_report(spec, result, deselect))
    shutil.rmtree(ws.root, ignore_errors=True)
    return result


def render_report(spec: str, result: RedTeamResult, struck: set[str]) -> str:
    lines = [
        "# Red-team report",
        "",
        f"- Rounds: {len(result.rounds)}",
        f"- Accepted attack files: {result.accepted_tests}",
        f"- Open breaks (unfixed): {result.open_breaks}",
        f"- Tests struck by the judge: {len(struck)}",
        "",
        "## Spec",
        "",
        "```",
        spec.strip(),
        "```",
        "",
        "## Rounds",
        "",
    ]
    for r in result.rounds:
        lines.append(f"### Round {r.number}: {r.outcome}")
        lines.append("")
        lines.append(f"Attack: {r.attack_summary}")
        if r.failures:
            lines.append("")
            lines.append("Breaks: " + ", ".join(f"`{f}`" for f in r.failures))
        for d in r.disputes:
            lines.append(f"- Dispute `{d['test']}` → **{d['verdict']}**: {d['reasoning']}")
        if r.notes:
            lines.append("")
            lines.append(f"Notes: {r.notes}")
        lines.append("")
    return "\n".join(lines)
