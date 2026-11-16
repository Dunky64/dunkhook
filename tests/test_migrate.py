import json
import subprocess
import textwrap

import pytest

from dunkhook.core.llm import ScriptedLLM
from dunkhook.migrate import Checklist, extract, migrate, normalise_path, plan

FLASK_BP = textwrap.dedent('''
    from flask import Blueprint, jsonify, abort

    bp = Blueprint("users", __name__, url_prefix="/users")
    USERS = {1: "ann"}

    @bp.route("/<int:user_id>")
    def get_user(user_id):
        if user_id not in USERS:
            abort(404)
        return jsonify(name=USERS[user_id])

    @bp.route("/", methods=["GET", "POST"])
    def users():
        return jsonify(list(USERS.values()))
''')

FASTAPI_BP = textwrap.dedent('''
    from fastapi import APIRouter, HTTPException

    bp = APIRouter(prefix="/users")
    USERS = {1: "ann"}

    @bp.get("/{user_id}")
    def get_user(user_id: int):
        if user_id not in USERS:
            raise HTTPException(status_code=404)
        return {"name": USERS[user_id]}

    @bp.api_route("/", methods=["GET", "POST"])
    def users():
        return list(USERS.values())
''')

FLASK_APP = textwrap.dedent('''
    from flask import Flask
    from users import bp

    app = Flask(__name__)
    app.register_blueprint(bp)

    @app.get("/health")
    def health():
        return {"ok": True}
''')

FASTAPI_APP_MISSING_ROUTE = "from fastapi import FastAPI\nfrom users import bp\n\napp = FastAPI()\napp.include_router(bp)\n"
FASTAPI_APP = FASTAPI_APP_MISSING_ROUTE + '\n@app.get("/health")\ndef health():\n    return {"ok": True}\n'


def test_normalise_path():
    assert normalise_path("/u/<int:id>/x/<name>") == normalise_path("/u/{id}/x/{name}") == "/u/{}/x/{}"
    assert normalise_path("/f/<path:p>") == normalise_path("/f/{p:path}") == "/f/{*}"


def test_route_extraction_matches_across_frameworks():
    before, after = extract(FLASK_BP), extract(FASTAPI_BP)
    assert before.routes == after.routes == {("GET", "/{}"), ("GET", "/"), ("POST", "/")}
    assert before.prefixes == after.prefixes == {"bp": "/users"}
    assert before.imports_flask and after.imports_fastapi and not after.imports_flask


@pytest.fixture
def app_repo(tmp_path):
    (tmp_path / "users.py").write_text(FLASK_BP)
    (tmp_path / "app.py").write_text(FLASK_APP)
    (tmp_path / "util.py").write_text("def helper():\n    return 1\n")
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"], ["add", "-A"], ["commit", "-qm", "flask app"]):
        subprocess.run(["git", *cmd], cwd=tmp_path, check=True)
    return tmp_path


def test_plan_orders_app_last(app_repo):
    cl = plan(app_repo)
    assert [i.path for i in cl.items] == ["users.py", "app.py"]
    assert cl.items[0].routes_before == ["GET /", "GET /{}", "POST /"]
    assert (app_repo / ".migration" / "CHECKLIST.md").exists()


def test_migrate_verifies_retries_and_commits(app_repo):
    prompts = []

    def convert(system, prompt, schema):
        prompts.append(prompt)
        if "File: users.py" in prompt:
            return {"code": FASTAPI_BP, "notes": "", "new_requirements": []}
        # First try drops a route; verifier must catch it and feed it back.
        code = FASTAPI_APP if "failed verification" in prompt else FASTAPI_APP_MISSING_ROUTE
        return {"code": code, "notes": "uvicorn needed to run", "new_requirements": ["uvicorn"]}

    cl = migrate(app_repo, ScriptedLLM(json_handler=convert), commit=True, test_cmd="python -c 'import ast'", log=lambda *_: None)
    assert [(i.path, i.status, i.attempts) for i in cl.items] == [("users.py", "done", 1), ("app.py", "done", 2)]
    assert "routes missing after migration: GET /health" in prompts[2]
    assert "Already migrated in this repo: users.py" in prompts[1]
    assert (app_repo / "app.py").read_text() == FASTAPI_APP
    log = subprocess.run(["git", "log", "--format=%s"], cwd=app_repo, capture_output=True, text=True).stdout
    assert "migrate: app.py from Flask to FastAPI" in log and "migrate: users.py from Flask to FastAPI" in log
    assert cl.final_test == "passed"
    assert "2/2 files migrated" in (app_repo / ".migration" / "CHECKLIST.md").read_text()


def test_failed_item_restores_original_and_resume_skips_done(app_repo):
    def convert(system, prompt, schema):
        if "File: users.py" in prompt:
            return {"code": "from flask import Blueprint\n", "notes": "", "new_requirements": []}
        return {"code": FASTAPI_APP, "notes": "", "new_requirements": []}

    llm = ScriptedLLM(json_handler=convert)
    cl = migrate(app_repo, llm, max_files=1, max_attempts=2, log=lambda *_: None)
    users = cl.items[0]
    assert users.status == "failed" and users.attempts == 2
    assert any("still imports flask" in p for p in users.problems)
    assert (app_repo / "users.py").read_text() == FLASK_BP
    assert cl.items[1].status == "pending"  # max_files=1

    cl = migrate(app_repo, llm, log=lambda *_: None)  # resumes from checklist.json
    assert [i.status for i in cl.items] == ["failed", "done"]
    saved = json.loads((app_repo / ".migration" / "checklist.json").read_text())
    assert [i["status"] for i in saved["items"]] == ["failed", "done"]
    assert Checklist.load(app_repo).items[1].path == "app.py"


def test_syntax_errors_are_caught(app_repo):
    llm = ScriptedLLM(json_handler=lambda *a: {"code": "def broken(:\n", "notes": "", "new_requirements": []})
    cl = migrate(app_repo, llm, max_files=1, max_attempts=1, log=lambda *_: None)
    assert cl.items[0].problems[0].startswith("syntax error")
