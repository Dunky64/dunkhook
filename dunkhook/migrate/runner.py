"""Flask → FastAPI migration, one file at a time, with a checklist and self-verification.

``plan`` scans the repo for Flask modules and writes ``.migration/checklist.json``
(plus a readable ``CHECKLIST.md``). ``run`` works through pending items:

1. Claude converts the file, following the mapping rules below.
2. Verifiers run on the result:
   * it parses (``compile``),
   * no ``flask`` imports remain,
   * route parity: same (METHOD, path) set and router prefixes as before,
   * optional per-file command (``--verify-cmd``, ``{file}`` is substituted).
3. Verifier failures go back to Claude; after ``max_attempts`` the original file
   is restored and the item is marked ``failed`` with the reasons.
4. The checklist is saved after every item, so runs are resumable. ``--commit``
   commits each migrated file separately.

A final ``--test-cmd`` runs once all items are processed, since a half-migrated
app usually can't pass its suite.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..core.gitutil import Git
from ..core.llm import LLM
from ..core.proc import quote, run
from ..core.repo_tools import iter_files
from ..core.schema import array, obj, string
from .routes import compare, extract

STATE_DIR = ".migration"

RULES = """Mapping rules (Flask → FastAPI):
- `Flask(__name__)` → `FastAPI()`; `Blueprint(name, __name__, url_prefix=P)` → `APIRouter(prefix=P)` (keep the SAME variable name).
- `app.register_blueprint(bp)` → `app.include_router(bp)`.
- `@x.route(path, methods=[...])` → one `@x.get/post/...(path)` per method (or `@x.api_route(path, methods=[...])`).
- Path converters: `<int:id>` → `{id}` with `id: int`; `<id>` → `{id}: str`; `<path:p>` → `{p:path}`.
- `request.get_json()` / `request.json` → a Pydantic model parameter when the shape is clear, else `payload: dict = Body(...)`.
- `request.args.get("k", d)` → a query parameter `k: type = d`. `request.form` → `Form(...)`. `request.files` → `UploadFile`.
- `request.headers` / cookies → `Header()` / `Cookie()` parameters, or `request: Request` when used dynamically.
- `jsonify(x)` → return `x`; `(body, status)` tuples → `status_code=` on the decorator or `JSONResponse(content, status_code=...)`.
- `abort(code)` → `raise HTTPException(status_code=code)`. `@app.errorhandler(E)` → `@app.exception_handler(E)` returning a JSONResponse.
- `flask.g` / `before_request` → dependencies (`Depends`) or middleware. `current_app.config` → a settings object or `app.state`.
- `redirect(url)` → `RedirectResponse(url)`; `url_for` → `request.url_for` with route `name=`s, or a literal path.
- `render_template` → `Jinja2Templates(directory=...).TemplateResponse(request, name, context)`.
- `app.run(...)` → `uvicorn.run(app, ...)` under the same `__main__` guard.
- Keep handler function names, behaviour, status codes and response shapes identical. Don't add routes or features.
- Keep functions sync (`def`) unless the original awaited something; FastAPI runs sync handlers in a threadpool."""

SYSTEM = f"""You migrate a Python codebase from Flask to FastAPI, one file at a time. Return the \
complete new contents of the file you are given. Preserve every route, status code, response shape and \
public name other modules may import. Leave non-Flask code untouched.

{RULES}"""

CONVERT_SCHEMA = obj(
    code=string("Complete new file contents"),
    notes=string("Anything a reviewer should check: behaviour that could not map 1:1, TODOs"),
    new_requirements=array(string("pip requirement, e.g. python-multipart")),
)


@dataclass
class Item:
    path: str
    status: str = "pending"  # pending | done | failed | skipped
    attempts: int = 0
    routes_before: list[str] = field(default_factory=list)
    notes: str = ""
    problems: list[str] = field(default_factory=list)
    new_requirements: list[str] = field(default_factory=list)
    commit: str | None = None


@dataclass
class Checklist:
    items: list[Item]
    final_test: str | None = None

    @classmethod
    def load(cls, root: Path) -> "Checklist":
        data = json.loads((root / STATE_DIR / "checklist.json").read_text())
        return cls([Item(**i) for i in data["items"]], data.get("final_test"))

    def save(self, root: Path) -> None:
        d = root / STATE_DIR
        d.mkdir(exist_ok=True)
        (d / "checklist.json").write_text(json.dumps({"items": [asdict(i) for i in self.items], "final_test": self.final_test}, indent=2))
        (d / "CHECKLIST.md").write_text(self.render())

    def render(self) -> str:
        marks = {"done": "[x]", "failed": "[!]", "skipped": "[-]", "pending": "[ ]"}
        done = sum(i.status == "done" for i in self.items)
        lines = ["# Flask → FastAPI migration checklist", "", f"{done}/{len(self.items)} files migrated", ""]
        for i in self.items:
            lines.append(f"- {marks[i.status]} `{i.path}` ({len(i.routes_before)} routes, {i.attempts} attempt(s))")
            for p in i.problems:
                lines.append(f"  - ⚠ {p}")
            if i.notes:
                lines.append(f"  - note: {i.notes}")
            if i.new_requirements:
                lines.append(f"  - needs: {', '.join(i.new_requirements)}")
        if self.final_test:
            lines += ["", f"Final test run: {self.final_test}"]
        return "\n".join(lines) + "\n"


def plan(repo: str | Path) -> Checklist:
    root = Path(repo).resolve()
    items: list[tuple[int, Item]] = []
    for f in iter_files(root):
        if f.suffix != ".py" or STATE_DIR in f.parts:
            continue
        src = f.read_text(errors="replace")
        if "flask" not in src.lower():
            continue
        try:
            table = extract(src)
        except SyntaxError:
            continue
        if not table.imports_flask:
            continue
        # Helpers first, then blueprints, then modules that create the app.
        rank = 2 if re.search(r"\bFlask\(", src) else (1 if table.routes else 0)
        items.append((rank, Item(str(f.relative_to(root)), routes_before=table.describe())))
    checklist = Checklist([i for _, i in sorted(items, key=lambda x: (x[0], x[1].path))])
    checklist.save(root)
    return checklist


def verify(original: str, new: str, verify_cmd: str | None, root: Path, rel: str) -> list[str]:
    try:
        compile(new, rel, "exec")
    except SyntaxError as exc:
        return [f"syntax error: {exc}"]
    before, after = extract(original), extract(new)
    problems = []
    if after.imports_flask:
        problems.append("file still imports flask")
    problems += compare(before, after)
    if verify_cmd and not problems:
        res = run(verify_cmd.replace("{file}", quote(rel)), cwd=root, timeout=600)
        if not res.ok:
            problems.append(f"verify command failed:\n{res.tail(3000)}")
    return problems


def migrate(
    repo: str | Path,
    llm: LLM,
    max_files: int | None = None,
    max_attempts: int = 3,
    verify_cmd: str | None = None,
    test_cmd: str | None = None,
    commit: bool = False,
    log=print,
) -> Checklist:
    root = Path(repo).resolve()
    state = root / STATE_DIR / "checklist.json"
    checklist = Checklist.load(root) if state.exists() else plan(root)
    git = Git(root) if commit else None
    processed = 0
    migrated_context: list[str] = []

    for item in checklist.items:
        if item.status != "pending":
            if item.status == "done":
                migrated_context.append(item.path)
            continue
        if max_files is not None and processed >= max_files:
            break
        processed += 1
        path = root / item.path
        original = path.read_text()
        feedback = ""
        log(f"[{item.path}] migrating ({len(item.routes_before)} routes)")
        for attempt in range(1, max_attempts + 1):
            item.attempts += 1
            resp = llm.json(
                SYSTEM,
                f"File: {item.path}\nAlready migrated in this repo: {', '.join(migrated_context) or 'nothing yet'}\n"
                f"Routes this file must still declare: {', '.join(item.routes_before) or 'none'}\n\n"
                f"```python\n{original}\n```\n{feedback}",
                CONVERT_SCHEMA,
            )
            problems = verify(original, resp["code"], None, root, item.path)
            if not problems and verify_cmd:
                path.write_text(resp["code"])
                problems = verify(original, resp["code"], verify_cmd, root, item.path)
            if not problems:
                path.write_text(resp["code"])
                item.status, item.notes, item.problems = "done", resp["notes"], []
                item.new_requirements = resp["new_requirements"]
                log(f"[{item.path}] verified on attempt {attempt}")
                break
            item.problems = problems
            log(f"[{item.path}] attempt {attempt} failed verification: {problems[0][:160]}")
            feedback = (
                "\nYour previous conversion failed verification:\n- " + "\n- ".join(problems)
                + f"\n\nPrevious attempt:\n```python\n{resp['code']}\n```\nFix these problems."
            )
        if item.status != "done":
            path.write_text(original)
            item.status = "failed"
            log(f"[{item.path}] giving up; original restored")
        else:
            migrated_context.append(item.path)
            if git is not None:
                item.commit = git.commit_all(f"migrate: {item.path} from Flask to FastAPI", [item.path])
        checklist.save(root)

    if test_cmd and all(i.status != "pending" for i in checklist.items):
        res = run(test_cmd, cwd=root, timeout=1800)
        checklist.final_test = "passed" if res.ok else "FAILED\n\n```\n" + res.tail(3000) + "\n```"
        log(f"final test run: {'passed' if res.ok else 'FAILED'}")
    checklist.save(root)
    return checklist
