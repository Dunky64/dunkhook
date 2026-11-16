"""DepBot against a fake project whose 'install' and 'tests' are tiny scripts.

The test command reads requirements.txt and fails for versions the fake
project can't handle, so no real packages are installed.
"""

import subprocess
import sys
import textwrap

import pytest

from dunkhook.core.llm import ScriptedLLM
from dunkhook.depbot import DepBot, StaticSource, read_pins, render_report
from dunkhook.depbot.bot import heuristic_next, set_pin


@pytest.fixture
def project(tmp_path):
    (tmp_path / "requirements.txt").write_text("# deps\nrequests==2.0.0  # http\nfoo[extra]==1.0.0\nbar==3.1\nunpinned>=1\n")
    (tmp_path / "check.py").write_text(textwrap.dedent("""
        import re, sys
        pins = dict(re.findall(r"^([\\w.-]+)(?:\\[.*?\\])?==(\\S+)", open("requirements.txt").read(), re.M))
        # foo 2.x removed an API we use; bar is fine with anything.
        if pins["foo"].startswith("2."):
            print("AttributeError: module 'foo' has no attribute 'old_api'")
            sys.exit(1)
        print("ok")
    """))
    for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    return tmp_path


VERSIONS = StaticSource({
    "requests": ["2.0.0", "2.31.0", "3.0.0b1"],
    "foo": ["1.0.0", "1.4.2", "2.0.0", "2.1.0"],
    "bar": ["3.1"],
})


def test_pins_roundtrip():
    text = "a==1\nb[x]==2.0 ; python_version>'3'\n"
    pins = read_pins(text)
    assert [(p.name, p.version, p.extras) for p in pins] == [("a", "1", ""), ("b", "2.0", "[x]")]
    assert set_pin(text, pins[1], "3.0") == "a==1\nb[x]==3.0 ; python_version>'3'\n"


def test_heuristic_steps_down_a_major():
    assert heuristic_next("2.1.0", ["2.0.0", "1.4.2"]) == "1.4.2"
    assert heuristic_next("1.4.2", ["1.4.1"]) is None


def test_bot_upgrades_reverts_and_uses_advice(project):
    def advise(system, prompt, schema):
        assert "old_api" in prompt
        return {"category": "breaking_api", "diagnosis": "foo 2 removed old_api", "next_version": "1.4.2",
                "migration_hint": "switch to foo.new_api"}

    llm = ScriptedLLM(json_handler=advise)
    bot = DepBot(project, f"{sys.executable} check.py", install_cmd="true", llm=llm, versions=VERSIONS,
                 commit=True, log=lambda *_: None)
    results = {r.package: r for r in bot.run()}

    assert results["requests"].status == "upgraded" and results["requests"].final == "2.31.0"  # prerelease skipped
    foo = results["foo"]
    assert foo.status == "upgraded" and foo.final == "1.4.2"
    assert [a.version for a in foo.attempts] == ["2.1.0", "1.4.2"]
    assert results["bar"].status == "up_to_date"

    req = (project / "requirements.txt").read_text()
    assert "requests==2.31.0  # http" in req and "foo[extra]==1.4.2" in req and "unpinned>=1" in req
    log = subprocess.run(["git", "log", "--format=%s"], cwd=project, capture_output=True, text=True).stdout
    assert "deps: bump foo from 1.0.0 to 1.4.2" in log and "deps: bump requests from 2.0.0 to 2.31.0" in log
    report = render_report(list(results.values()))
    assert "breaking_api" in report and "foo.new_api" in report


def test_bot_reverts_when_advisor_gives_up(project):
    llm = ScriptedLLM(json_handler=lambda *a: {"category": "breaking_api", "diagnosis": "x", "next_version": None, "migration_hint": ""})
    bot = DepBot(project, f"{sys.executable} check.py", install_cmd="true", llm=llm, versions=VERSIONS, log=lambda *_: None)
    foo = next(r for r in bot.run(only=["foo"]))
    assert foo.status == "reverted" and foo.final == "1.0.0"
    assert "foo[extra]==1.0.0" in (project / "requirements.txt").read_text()


def test_bot_refuses_red_baseline(project):
    (project / "requirements.txt").write_text("foo==2.0.0\n")
    bot = DepBot(project, f"{sys.executable} check.py", install_cmd="true", versions=VERSIONS, log=lambda *_: None)
    with pytest.raises(RuntimeError, match="baseline"):
        bot.run()
