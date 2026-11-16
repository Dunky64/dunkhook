import subprocess

import pytest

from dunkhook.archaeologist import Target, investigate, render
from dunkhook.core.llm import ScriptedLLM


def git(repo, *args, env=None):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env).stdout


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "a@example.com")
    git(tmp_path, "config", "user.name", "Alice")
    f = tmp_path / "retry.py"
    f.write_text("def fetch():\n    return get()\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "Add fetch")
    f.write_text("def fetch():\n    time.sleep(0.25)  # upstream rate limit\n    return get()\n")
    git(tmp_path, "commit", "-qam", "Back off before fetch\n\nUpstream returns 429 under burst load. Fixes #42")
    f.write_text("import time\n\n\ndef fetch():\n    time.sleep(0.25)  # upstream rate limit\n    return get()\n")
    git(tmp_path, "commit", "-qam", "Add missing import")
    return tmp_path


def test_target_parse():
    assert Target.parse("a/b.py:3") == Target("a/b.py", 3, 3)
    assert Target.parse("a/b.py:3-9") == Target("a/b.py", 3, 9)
    with pytest.raises(ValueError):
        Target.parse("a/b.py")


def test_investigate_gives_agent_blame_refs_and_working_tools(repo):
    seen = {}

    def agent(system, prompt, tools):
        seen["prompt"] = prompt
        seen["tools"] = sorted(tools)
        history = tools["line_history"].fn({"path": "retry.py", "start_line": 5, "end_line": 5, "max_commits": 5})
        assert "Upstream returns 429" in history
        pick = tools["pickaxe"].fn({"text": "time.sleep(0.25)", "path": ""})
        assert "Back off before fetch" in pick
        sha = pick.split()[0]
        assert "Fixes #42" in tools["show_commit"].fn({"sha": sha})
        assert "Back off" in tools["grep_log"].fn({"pattern": "back ?off"})
        assert "time.sleep" not in tools["read_file_at"].fn({"path": "retry.py", "rev": "HEAD~2", "start_line": 0, "end_line": 0})
        return {"answer": "Throttles calls because upstream 429s under burst.", "confidence": "high", "origin_commit": sha,
                "evidence": [{"kind": "commit", "ref": sha, "note": "adds sleep, Fixes #42"}], "still_needed": "unclear",
                "open_questions": ["Is the upstream limit still in place?"]}

    result = investigate(repo, ScriptedLLM(agent_handler=agent), target="retry.py:5", token="")
    assert "Back off before fetch" in seen["prompt"] and "#42" in seen["prompt"]
    assert ">    5      time.sleep(0.25)" in seen["prompt"]
    assert "NOT available" in seen["prompt"]
    assert "github_issue" not in seen["tools"]  # no token, no GitHub tools
    md = render(result)
    assert "Why is `retry.py:5-5` here?" in md and "Open questions" in md


def test_symbol_lookup(repo):
    result = investigate(repo, ScriptedLLM(agent_handler=lambda s, p, t: {
        "answer": p, "confidence": "low", "origin_commit": "", "evidence": [], "still_needed": "unclear", "open_questions": []}),
        symbol="fetch", token="")
    assert result["target"] == "retry.py:4-4"
