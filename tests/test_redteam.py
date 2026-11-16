import json

from dunkhook.core.llm import ScriptedLLM
from dunkhook.redteam import harness, run_redteam

SPEC = """Implement `clamp(x, lo, hi)` returning x limited to the inclusive range [lo, hi].
If lo > hi raise ValueError."""

BUGGY = "def clamp(x, lo, hi):\n    return max(lo, min(x, hi))\n"
FIXED = (
    "def clamp(x, lo, hi):\n"
    "    if lo > hi:\n        raise ValueError('lo > hi')\n"
    "    return max(lo, min(x, hi))\n"
)

ATTACKS = [
    # round 1: real break (lo > hi must raise)
    "import pytest\nfrom solution import clamp\n\n"
    "def test_inverted_range_raises():\n    with pytest.raises(ValueError):\n        clamp(1, 5, 0)\n\n"
    "def test_basic():\n    assert clamp(7, 0, 5) == 5\n",
    # round 2: one out-of-spec test (will be disputed and struck) + one valid one
    "from solution import clamp\n\n"
    "def test_strings_rejected():\n    assert clamp('a', 'a', 'z') is None\n\n"
    "def test_equal_bounds():\n    assert clamp(3, 2, 2) == 2\n",
    # round 3: doesn't even import -> rejected attack
    "from solution import clamp\nthis is not python\n",
    # round 4: survives
    "from solution import clamp\n\ndef test_inside():\n    assert clamp(3, 0, 5) == 3\n",
    # round 5: unfixable demand -> open break, rollback
    "from solution import clamp\n\ndef test_impossible():\n    assert clamp(1, 0, 5) == 99\n",
]


def make_llm():
    state = {"attack": 0, "fix": 0}

    def handler(system, prompt, schema):
        if system == harness.BREAKER_SYSTEM:
            code = ATTACKS[state["attack"]]
            state["attack"] += 1
            return {"test_code": code, "attack_summary": f"attack {state['attack']}"}
        if system == harness.JUDGE_SYSTEM:
            return {"verdict": "invalid", "reasoning": "spec says nothing about strings"}
        # builder
        if "test_strings_rejected" in prompt and "These tests fail" in prompt:
            return {"code": FIXED, "disputes": [{"test": "test_strings_rejected", "reason": "not in spec"}], "notes": "dispute"}
        if "test_impossible" in prompt:
            return {"code": "def clamp(x, lo, hi):\n    return 99\n", "disputes": [], "notes": "caved"}
        return {"code": FIXED, "disputes": [], "notes": "raise on inverted range"}

    return ScriptedLLM(json_handler=handler)


def test_full_redteam_loop(tmp_path):
    out = tmp_path / "out"
    res = run_redteam(SPEC, make_llm(), out, rounds=5, fix_attempts=1, initial_code=BUGGY, log=lambda *_: None)
    outcomes = [r.outcome for r in res.rounds]
    assert outcomes == ["patched", "patched", "rejected_attack", "survived", "open_break"]
    # Survivor is the fixed code, not the round-5 "caved" version.
    assert (out / "solution.py").read_text() == FIXED
    # Accepted tests are the ones that survived; the open break is quarantined.
    assert sorted(p.name for p in (out / "tests").glob("test_*.py")) == [
        "test_round_1.py", "test_round_2.py", "test_round_4.py"
    ]
    assert (out / "open_breaks" / "test_round_5.py").exists()
    assert json.loads((out / "struck_tests.json").read_text()) == ["tests/test_round_2.py::test_strings_rejected"]
    assert res.open_breaks == 1 and not res.survived_all
    report = (out / "report.md").read_text()
    assert "Round 2: patched" in report and "invalid" in report


def test_survivor_suite_runs_standalone(tmp_path):
    out = tmp_path / "out"
    run_redteam(SPEC, make_llm(), out, rounds=2, fix_attempts=1, initial_code=BUGGY, log=lambda *_: None)
    from dunkhook.core.proc import run
    import sys

    struck = json.loads((out / "struck_tests.json").read_text())
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
    for node in struck:
        cmd += ["--deselect", node]
    assert run(cmd, cwd=out).ok
