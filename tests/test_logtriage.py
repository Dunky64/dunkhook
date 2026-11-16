import json
import subprocess

from dunkhook.core.llm import ScriptedLLM
from dunkhook.logtriage import apply_edits, cluster_events, parse_lines, propose_fix, render_clusters

LOG = """\
2024-05-01 10:00:01 INFO request ok
2024-05-01 10:00:02 ERROR unhandled exception
Traceback (most recent call last):
  File "/srv/app/views.py", line 10, in get_user
    return users[int(user_id)]
  File "/srv/app/store.py", line 4, in lookup
    raise KeyError(user_id)
KeyError: '8f14e45f-ceea-467a-9575-6b2b1c1e1e1e'
2024-05-01 10:05:00 ERROR unhandled exception
Traceback (most recent call last):
  File "/srv/app/views.py", line 10, in get_user
    return users[int(user_id)]
  File "/srv/app/store.py", line 4, in lookup
    raise KeyError(user_id)
KeyError: '1c1e1e1e-ceea-467a-9575-6b2b8f14e45f'
2024-05-01 10:06:00 ERROR payment gateway timeout after 3012 ms for order 991
2024-05-01 10:07:00 ERROR payment gateway timeout after 5020 ms for order 1234
{"level": "error", "message": "db pool exhausted", "timestamp": "2024-05-01T10:08:00Z", "exc_info": "Traceback (most recent call last):\\n  File \\"/srv/app/db.py\\", line 3, in connect\\n    pool.get()\\nTimeoutError: pool exhausted"}
Traceback (most recent call last):
  File "/srv/app/views.py", line 20, in parse
    int(x)
ValueError: invalid literal for int() with base 10: 'abc'

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/srv/app/views.py", line 22, in parse
    raise BadRequest()
app.errors.BadRequest
"""


def test_parse_and_cluster():
    events = parse_lines(LOG.splitlines())
    types = [e.exc_type for e in events]
    assert types == ["KeyError", "KeyError", "LogError", "LogError", "TimeoutError", "app.errors.BadRequest"]
    clusters = cluster_events(events)
    assert [(c.exc_type, c.count) for c in clusters][:2] == [("KeyError", 2), ("LogError", 2)]
    assert len(clusters) == 4
    assert clusters[0].first_seen == "2024-05-01 10:00:02" and clusters[0].last_seen == "2024-05-01 10:05:00"
    md = render_clusters(clusters)
    assert "KeyError` × 2" in md and "store.py:4 in lookup" in md


def test_apply_edits_is_atomic(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\nx = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    import pytest

    with pytest.raises(ValueError):
        apply_edits(tmp_path, [{"path": "b.py", "old": "y = 2", "new": "y = 3"}, {"path": "a.py", "old": "x = 1", "new": "x = 2"}])
    assert (tmp_path / "b.py").read_text() == "y = 2\n"  # nothing applied
    with pytest.raises(ValueError):
        apply_edits(tmp_path, [{"path": "../evil.py", "old": "", "new": "boom"}])
    assert apply_edits(tmp_path, [{"path": "b.py", "old": "y = 2", "new": "y = 3"}, {"path": "new/c.py", "old": "", "new": "z"}]) == ["b.py", "new/c.py"]


def _repo(tmp_path):
    (tmp_path / "store.py").write_text("users = {}\n\ndef lookup(user_id):\n    return users[user_id]\n")
    (tmp_path / "test_store.py").write_text("from store import lookup\n\ndef test_missing():\n    assert lookup('nope') is None\n")
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@t"], ["git", "config", "user.name", "t"],
                ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=tmp_path, check=True)
    return tmp_path


def test_propose_fix_retries_after_failing_tests(tmp_path):
    repo = _repo(tmp_path)
    cluster = cluster_events(parse_lines(LOG.splitlines()))[0]
    attempts = []

    def agent(system, prompt, tools):
        # The agent can really use the read-only tools.
        assert "def lookup" in tools["read_file"].fn({"path": "store.py", "start_line": 0, "end_line": 0})
        assert "store.py:3" in tools["search_code"].fn({"pattern": "def lookup", "glob": "*.py"})
        attempts.append(prompt)
        new = "    return users[user_id] or None\n" if len(attempts) == 1 else "    return users.get(user_id)\n"
        return {"root_cause": "unknown ids raise KeyError", "confidence": "high",
                "edits": [{"path": "store.py", "old": "    return users[user_id]\n", "new": new}],
                "pr_title": "Return None for unknown users", "pr_body": "Use dict.get."}

    out = propose_fix(repo, cluster, ScriptedLLM(agent_handler=agent), test_cmd="python -m pytest -q -p no:cacheprovider",
                      log=lambda *_: None)
    assert out.status == "proposed" and out.branch == f"triage/{cluster.id}"
    assert len(attempts) == 2 and "broke the tests" in attempts[1]
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True).stdout
    assert git("rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert "users[user_id]" in (repo / "store.py").read_text()  # main untouched
    assert "users.get(user_id)" in git("show", f"{out.branch}:store.py")
    body = (repo / ".triage" / f"{cluster.id}.md").read_text()
    assert "Use dict.get." in body and "passes on this branch" in body
    assert json.loads(body.split("```json\n")[1].split("```")[0])["count"] == 2


def test_propose_fix_without_edits_cleans_up(tmp_path):
    repo = _repo(tmp_path)
    cluster = cluster_events(parse_lines(LOG.splitlines()))[0]
    agent = lambda *a: {"root_cause": "?", "confidence": "low", "edits": [], "pr_title": "", "pr_body": ""}
    out = propose_fix(repo, cluster, ScriptedLLM(agent_handler=agent), log=lambda *_: None)
    assert out.status == "no_fix"
    branches = subprocess.run(["git", "branch"], cwd=repo, capture_output=True, text=True).stdout
    assert "triage/" not in branches
