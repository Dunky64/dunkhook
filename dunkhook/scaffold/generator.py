"""OpenAPI 3 → typed Python client, mock server and integration tests.

Code generation is deterministic (no model call). The output is verified by
running the generated integration tests against the generated mock. With an LLM
attached, Claude then writes an extra edge-case test file in a loop:
generate → run → feed failures back. If the file still fails after
``edge_case_rounds`` tries it is dropped, so only passing tests are kept.

Output layout::

    <out>/<package>/__init__.py     models + client re-exports
    <out>/<package>/models.py       dataclasses for components.schemas
    <out>/<package>/client.py       one method per operation (urllib, no deps)
    <out>/<package>/_runtime.py     embedded spec, fake data, validator, mock
    <out>/<package>/mock_server.py  `python -m <package>.mock_server 8080`
    <out>/tests/conftest.py
    <out>/tests/test_integration.py
    <out>/tests/test_edge_cases.py  (optional, model-written, verified)
"""

from __future__ import annotations

import inspect
import json
import keyword
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.llm import LLM
from ..core.proc import run
from ..core.schema import obj, string
from . import runtime_template
from .runtime_template import fake, json_body_schema, resolve, success_response

METHODS = ("get", "post", "put", "patch", "delete")


def load_spec(path: str | Path) -> dict:
    text = Path(path).read_text()
    if str(path).endswith((".yaml", ".yml")):
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise SystemExit("YAML specs need PyYAML: pip install pyyaml (or pass a JSON spec)") from exc
        return yaml.safe_load(text)
    return json.loads(text)


# -- naming ---------------------------------------------------------------------

def snake(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"_+", "_", s).strip("_").lower() or "value"
    if s[0].isdigit():
        s = "_" + s
    return s + "_" if keyword.iskeyword(s) or s in {"self", "body", "query"} else s


def pascal(name: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", name)
    s = "".join(p[:1].upper() + p[1:] for p in parts if p) or "Model"
    return "_" + s if s[0].isdigit() else s


def ident(name: str) -> str:
    return name if name.isidentifier() and not keyword.iskeyword(name) else snake(name)


# -- type mapping -----------------------------------------------------------------

@dataclass
class Ctx:
    spec: dict
    models: dict[str, str] = field(default_factory=dict)  # "#/components/schemas/X" -> ClassName

    def model_for(self, schema: dict | None) -> str | None:
        if schema and "$ref" in schema:
            return self.models.get(schema["$ref"])
        return None


def is_object(schema: dict) -> bool:
    return schema.get("type") == "object" or ("properties" in schema and "type" not in schema)


def py_type(schema: dict | None, ctx: Ctx, prefix: str = "") -> str:
    if not schema:
        return "Any"
    model = ctx.model_for(schema)
    if model:
        return prefix + model
    s = resolve(schema, ctx.spec)
    if any(k in s for k in ("oneOf", "anyOf", "allOf")):
        return "Any"
    t = s.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    if t == "array":
        return f"List[{py_type(s.get('items'), ctx, prefix)}]"
    return {"string": "str", "integer": "int", "number": "float", "boolean": "bool", "object": "Dict[str, Any]"}.get(t, "Any")


def conv_expr(schema: dict | None, var: str, ctx: Ctx, prefix: str = "") -> str:
    """Expression turning decoded JSON ``var`` into the typed value."""
    model = ctx.model_for(schema)
    if model:
        return f"{prefix}{model}.from_dict({var}) if {var} is not None else None"
    s = resolve(schema, ctx.spec) if schema else {}
    if s.get("type") == "array":
        inner = ctx.model_for(s.get("items"))
        if inner:
            return f"[{prefix}{inner}.from_dict(x) for x in {var}] if {var} is not None else None"
    return var


# -- models.py --------------------------------------------------------------------

def gen_models(ctx: Ctx) -> str:
    schemas = ctx.spec.get("components", {}).get("schemas", {})
    out = [
        '"""Models generated from the OpenAPI spec. Do not edit by hand."""',
        "",
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "from typing import Any, Dict, List, Optional",
        "",
        "",
        "def _dump(v: Any) -> Any:",
        "    if hasattr(v, 'to_dict'):",
        "        return v.to_dict()",
        "    if isinstance(v, list):",
        "        return [_dump(x) for x in v]",
        "    if isinstance(v, dict):",
        "        return {k: _dump(x) for k, x in v.items()}",
        "    return v",
        "",
    ]
    for name, raw in schemas.items():
        cls = ctx.models.get(f"#/components/schemas/{name}")
        s = resolve(raw, ctx.spec)
        if cls is None:
            out += ["", f"{pascal(name)} = {py_type(raw, ctx)}  # schema alias", ""]
            continue
        props: dict = s.get("properties", {})
        required = set(s.get("required", []))
        fields = [(ident(k), k, v, k in required) for k, v in props.items()]
        fields.sort(key=lambda f: not f[3])  # required first (dataclass ordering)
        out += ["", "@dataclass", f"class {cls}:"]
        if s.get("description"):
            out.append(f"    {json.dumps(s['description'])}")
        if not fields:
            out.append("    pass")
        for py, _, sub, req in fields:
            t = py_type(sub, ctx)
            out.append(f"    {py}: {t}" if req else f"    {py}: Optional[{t}] = None")
        out += [
            "",
            "    _KEYS = {" + ", ".join(f"{py!r}: {key!r}" for py, key, _, _ in fields) + "}",
            "",
            "    @classmethod",
            f"    def from_dict(cls, d: Dict[str, Any]) -> \"{cls}\":",
        ]
        if fields:
            for py, key, sub, req in fields:
                getter = f"d[{key!r}]" if req else f"d.get({key!r})"
                out.append(f"        _{py} = {getter}")
            args = ", ".join(f"{py}={conv_expr(sub, '_' + py, ctx)}" for py, _, sub, _ in fields)
            out.append(f"        return cls({args})")
        else:
            out.append("        return cls()")
        out += [
            "",
            "    def to_dict(self) -> Dict[str, Any]:",
            "        return {key: _dump(getattr(self, py)) for py, key in self._KEYS.items() if getattr(self, py) is not None}",
            "",
        ]
    return "\n".join(out) + "\n"


# -- client.py --------------------------------------------------------------------

@dataclass
class Param:
    py: str
    name: str
    where: str  # path | query | body
    schema: dict | None
    required: bool


@dataclass
class Operation:
    method: str
    path: str
    py: str
    summary: str
    params: list[Param]
    body: Param | None
    status: int
    response: dict | None


def collect_operations(ctx: Ctx) -> list[Operation]:
    ops: list[Operation] = []
    used: set[str] = set()
    for path, item in ctx.spec.get("paths", {}).items():
        for method in METHODS:
            op = item.get(method)
            if op is None:
                continue
            py = snake(op.get("operationId") or f"{method}_{path}")
            while py in used:
                py += "_"
            used.add(py)
            params: list[Param] = []
            names: set[str] = set()
            for p in item.get("parameters", []) + op.get("parameters", []):
                p = resolve(p, ctx.spec)
                if p.get("in") not in ("path", "query"):
                    continue
                pname = snake(p["name"])
                while pname in names:
                    pname += "_"
                names.add(pname)
                params.append(Param(pname, p["name"], p["in"], p.get("schema"), p.get("in") == "path" or bool(p.get("required"))))
            body_schema, body_required = json_body_schema(op, ctx.spec)
            body = Param("body", "body", "body", body_schema, body_required) if body_schema is not None else None
            status, response = success_response(op)
            ops.append(Operation(method, path, py, op.get("summary") or op.get("description") or "", params, body, status, response))
    return ops


def gen_client(ctx: Ctx, ops: list[Operation], class_name: str) -> str:
    out = [
        '"""HTTP client generated from the OpenAPI spec. Do not edit by hand."""',
        "",
        "from __future__ import annotations",
        "",
        "import json",
        "import urllib.error",
        "import urllib.parse",
        "import urllib.request",
        "from typing import Any, Dict, List, Optional, Union",
        "",
        "from . import models",
        "from .models import _dump",
        "",
        "",
        "class ApiError(Exception):",
        "    def __init__(self, status: int, body: Any):",
        "        super().__init__(f'HTTP {status}: {body}')",
        "        self.status = status",
        "        self.body = body",
        "",
        "",
        "def _q(v: Any) -> str:",
        "    return urllib.parse.quote(str(v), safe='')",
        "",
        "",
        f"class {class_name}:",
        "    def __init__(self, base_url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 30.0):",
        "        self.base_url = base_url.rstrip('/')",
        "        self.headers = dict(headers or {})",
        "        self.timeout = timeout",
        "",
        "    def _request(self, method: str, path: str, query: Optional[Dict[str, Any]] = None, json_body: Any = None) -> Any:",
        "        url = self.base_url + path",
        "        q = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in (query or {}).items() if v is not None}",
        "        if q:",
        "            url += '?' + urllib.parse.urlencode(q, doseq=True)",
        "        data = None if json_body is None else json.dumps(json_body).encode()",
        "        headers = {'Accept': 'application/json', **self.headers}",
        "        if data is not None:",
        "            headers['Content-Type'] = 'application/json'",
        "        req = urllib.request.Request(url, data=data, method=method, headers=headers)",
        "        try:",
        "            with urllib.request.urlopen(req, timeout=self.timeout) as resp:",
        "                raw = resp.read()",
        "        except urllib.error.HTTPError as e:",
        "            raw = e.read()",
        "            try:",
        "                body = json.loads(raw) if raw else None",
        "            except ValueError:",
        "                body = raw.decode(errors='replace')",
        "            raise ApiError(e.code, body) from None",
        "        return json.loads(raw) if raw else None",
    ]
    for op in ops:
        ordered = [p for p in op.params if p.required]
        if op.body and op.body.required:
            ordered.append(op.body)
        ordered += [p for p in op.params if not p.required]
        if op.body and not op.body.required:
            ordered.append(op.body)
        sig = ["self"]
        for p in ordered:
            t = py_type(p.schema, ctx, "models.")
            if p.where == "body" and ctx.model_for(p.schema):
                t = f"Union[{t}, Dict[str, Any]]"
            sig.append(f"{p.py}: {t}" if p.required else f"{p.py}: Optional[{t}] = None")
        ret = "None" if op.response is None or op.status == 204 else py_type(op.response, ctx, "models.")
        pieces = re.split(r"\{([^}]+)\}", op.path)
        path_expr = []
        by_name = {p.name: p for p in op.params if p.where == "path"}
        for i, chunk in enumerate(pieces):
            if i % 2:
                path_expr.append(f"_q({by_name[chunk].py})" if chunk in by_name else repr("{" + chunk + "}"))
            elif chunk:
                path_expr.append(repr(chunk))
        query = ", ".join(f"{p.name!r}: {p.py}" for p in op.params if p.where == "query")
        call = f"self._request({op.method.upper()!r}, {' + '.join(path_expr) or repr('/')}"
        if query:
            call += f", query={{{query}}}"
        if op.body:
            call += ", json_body=_dump(body)"
        call += ")"
        out += ["", f"    def {op.py}({', '.join(sig)}) -> {ret}:"]
        doc = f"{op.method.upper()} {op.path}" + (f" - {op.summary}" if op.summary else "")
        out.append(f"        {json.dumps(doc)}")
        if ret == "None":
            out.append(f"        {call}")
        else:
            out.append(f"        data = {call}")
            out.append(f"        return {conv_expr(op.response, 'data', ctx, 'models.')}")
    out += ["", "", f"Client = {class_name}", ""]
    return "\n".join(out)


# -- tests ----------------------------------------------------------------------------

def _concrete_path(op: Operation, spec: dict, overrides: dict[str, str] | None = None) -> str:
    by_name = {p.name: p for p in op.params if p.where == "path"}
    def repl(m: re.Match) -> str:
        name = m.group(1)
        if overrides and name in overrides:
            return overrides[name]
        p = by_name.get(name)
        return str(fake(p.schema if p else {"type": "string"}, spec, name=name))
    return re.sub(r"\{([^}]+)\}", repl, op.path)


def gen_tests(ctx: Ctx, ops: list[Operation], pkg: str) -> str:
    out = [
        '"""Integration tests: generated client against the generated mock server."""',
        "",
        "import pytest",
        "",
        f"from {pkg} import models",
        f"from {pkg}.client import ApiError",
        "",
    ]
    for op in ops:
        args = []
        for p in op.params:
            if p.required:
                args.append(f"{p.py}={fake(p.schema, ctx.spec, name=p.name)!r}")
        if op.body and op.body.required or op.body and op.body.schema is not None:
            value = fake(op.body.schema, ctx.spec)
            model = ctx.model_for(op.body.schema)
            args.append(f"body=models.{model}.from_dict({value!r})" if model else f"body={value!r}")
        out += ["", f"def test_{op.py}(client):", f"    result = client.{op.py}({', '.join(args)})"]
        model = ctx.model_for(op.response)
        resolved = resolve(op.response, ctx.spec) if op.response else {}
        inner = ctx.model_for(resolved.get("items")) if resolved.get("type") == "array" else None
        if op.response is None or op.status == 204:
            out.append("    assert result is None")
        elif model:
            out.append(f"    assert isinstance(result, models.{model})")
        elif inner:
            out.append("    assert isinstance(result, list)")
            out.append(f"    assert all(isinstance(x, models.{inner}) for x in result)")
        else:
            out.append("    assert result is not None")

        body_s = resolve(op.body.schema, ctx.spec) if op.body else {}
        if op.body and (op.body.required or body_s.get("required")):
            payload = {} if body_s.get("required") else None
            out += [
                "",
                f"def test_{op.py}_rejects_invalid_body(client):",
                "    with pytest.raises(ApiError) as err:",
                f"        client._request({op.method.upper()!r}, {_concrete_path(op, ctx.spec)!r}, json_body={payload!r})",
                "    assert err.value.status == 422",
            ]
        for p in op.params:
            if p.where == "path" and resolve(p.schema, ctx.spec).get("type") == "integer":
                bad = _concrete_path(op, ctx.spec, {p.name: "not-a-number"})
                out += [
                    "",
                    f"def test_{op.py}_rejects_non_integer_{p.py}(client):",
                    "    with pytest.raises(ApiError) as err:",
                    f"        client._request({op.method.upper()!r}, {bad!r})",
                    "    assert err.value.status == 422",
                ]
                break
    out += [
        "",
        "def test_unknown_route_is_404(client):",
        "    with pytest.raises(ApiError) as err:",
        "        client._request('GET', '/__definitely_not_a_route__')",
        "    assert err.value.status == 404",
        "",
    ]
    return "\n".join(out)


# -- orchestration ----------------------------------------------------------------------

EDGE_SYSTEM = """You write pytest integration tests for a generated API client that runs against a \
generated mock server. The mock returns deterministic example data for 2xx responses, 422 for \
missing/mistyped required body fields, missing required query params and non-integer integer path \
params, 405 for wrong methods on a known path and 404 for unknown paths. Write tests that exercise \
edge cases of the client (query encoding, optional params, nested models, round-tripping to_dict/\
from_dict, error bodies) that MUST pass against this mock. Use the `client` fixture and import from \
the package. Do not start servers yourself."""

EDGE_SCHEMA = obj(test_code=string("Complete pytest file"), summary=string())


@dataclass
class ScaffoldResult:
    out_dir: Path
    package: str
    operations: int
    models: int
    tests_passed: bool
    test_output: str
    edge_cases_kept: bool | None = None


def _pytest(out: Path, *targets: str) -> tuple[bool, str]:
    res = run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets], cwd=out, timeout=300)
    return res.ok, res.output


def scaffold(
    spec: dict,
    out_dir: str | Path,
    package: str | None = None,
    llm: LLM | None = None,
    edge_case_rounds: int = 3,
    verify: bool = True,
    log=print,
) -> ScaffoldResult:
    out = Path(out_dir)
    title = spec.get("info", {}).get("title", "api")
    pkg = package or snake(title) + "_client"
    class_name = pascal(title) + "Client"
    ctx = Ctx(spec)
    for name, raw in spec.get("components", {}).get("schemas", {}).items():
        if is_object(resolve(raw, spec)):
            ctx.models[f"#/components/schemas/{name}"] = pascal(name)
    ops = collect_operations(ctx)

    pkg_dir = out / pkg
    tests_dir = out / "tests"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    tests_dir.mkdir(parents=True, exist_ok=True)

    runtime_src = inspect.getsource(runtime_template).replace(
        "SPEC: dict = {}  # replaced in generated copies",
        f"SPEC: dict = json.loads({json.dumps(json.dumps(spec))})",
    )
    (pkg_dir / "_runtime.py").write_text(runtime_src)
    (pkg_dir / "models.py").write_text(gen_models(ctx))
    (pkg_dir / "client.py").write_text(gen_client(ctx, ops, class_name))
    (pkg_dir / "__init__.py").write_text(
        f'"""Generated client for {title}."""\n\nfrom . import models\nfrom .client import ApiError, Client, {class_name}\n\n'
        f'__all__ = ["models", "ApiError", "Client", "{class_name}"]\n'
    )
    (pkg_dir / "mock_server.py").write_text(
        '"""Run the mock: python -m {pkg}.mock_server [port]"""\n\nimport sys\nimport time\n\n'
        "from ._runtime import start_mock\n\n"
        'if __name__ == "__main__":\n'
        "    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080\n"
        "    server, url = start_mock(port=port)\n"
        '    print(f"mock listening on {url}")\n'
        "    try:\n        while True:\n            time.sleep(3600)\n"
        "    except KeyboardInterrupt:\n        server.shutdown()\n".replace("{pkg}", pkg)
    )
    (tests_dir / "conftest.py").write_text(
        "import pathlib\nimport sys\n\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))\n\n"
        f"import pytest  # noqa: E402\n\nfrom {pkg}._runtime import start_mock  # noqa: E402\nfrom {pkg}.client import Client  # noqa: E402\n\n\n"
        '@pytest.fixture(scope="session")\ndef mock_url():\n    server, url = start_mock()\n    yield url\n    server.shutdown()\n\n\n'
        "@pytest.fixture\ndef client(mock_url):\n    return Client(mock_url)\n"
    )
    (tests_dir / "test_integration.py").write_text(gen_tests(ctx, ops, pkg))
    log(f"generated {pkg}: {len(ops)} operations, {len(ctx.models)} models")

    result = ScaffoldResult(out, pkg, len(ops), len(ctx.models), True, "")
    if not verify:
        return result
    ok, output = _pytest(out, "tests/test_integration.py")
    result.tests_passed, result.test_output = ok, output
    log("generated tests " + ("pass" if ok else "FAIL"))

    if llm is not None and ok:
        edge = tests_dir / "test_edge_cases.py"
        feedback = ""
        result.edge_cases_kept = False
        for attempt in range(1, edge_case_rounds + 1):
            resp = llm.json(
                EDGE_SYSTEM,
                f"Package name: {pkg}\n\nOpenAPI spec:\n```json\n{json.dumps(spec, indent=1)}\n```\n\n"
                f"client.py:\n```python\n{(pkg_dir / 'client.py').read_text()}\n```\n\n"
                f"models.py:\n```python\n{(pkg_dir / 'models.py').read_text()}\n```\n\n{feedback}",
                EDGE_SCHEMA,
            )
            edge.write_text(resp["test_code"])
            passed, edge_out = _pytest(out, "tests/test_edge_cases.py")
            if passed:
                result.edge_cases_kept = True
                log(f"edge-case tests pass (attempt {attempt}): {resp['summary']}")
                break
            log(f"edge-case tests failed (attempt {attempt}); feeding back")
            feedback = (
                f"Your previous file:\n```python\n{resp['test_code']}\n```\nfailed:\n```\n{edge_out[-5000:]}\n```\n"
                "Fix the tests (the client and mock are correct by construction; adjust expectations to match them)."
            )
        if not result.edge_cases_kept:
            edge.unlink(missing_ok=True)
            log("edge-case tests dropped (never passed)")
    return result
