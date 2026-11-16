"""Runtime shared by generated scaffolds (copied verbatim into the output as ``_runtime.py``).

Holds: $ref resolution, deterministic fake data from JSON schemas, a light
validator, and the mock HTTP server driven by the embedded OpenAPI document.
Standard library only.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

SPEC: dict = {}  # replaced in generated copies


def resolve(schema: dict | None, spec: dict | None = None) -> dict:
    spec = spec if spec is not None else SPEC
    seen = 0
    while schema and "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise ValueError(f"only local $refs are supported: {ref}")
        node: Any = spec
        for part in ref[2:].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        schema = node
        seen += 1
        if seen > 50:
            raise ValueError(f"$ref cycle at {ref}")
    return schema or {}


def fake(schema: dict | None, spec: dict | None = None, depth: int = 0, name: str = "") -> Any:
    """Deterministic example value for a schema."""
    s = resolve(schema, spec)
    if "example" in s:
        return s["example"]
    if "default" in s:
        return s["default"]
    if "enum" in s:
        return s["enum"][0]
    for key in ("allOf",):
        if key in s:
            out: dict = {}
            for sub in s[key]:
                v = fake(sub, spec, depth + 1, name)
                if isinstance(v, dict):
                    out.update(v)
            return out
    for key in ("oneOf", "anyOf"):
        if key in s:
            return fake(s[key][0], spec, depth + 1, name)
    t = s.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "null")
    if t is None:
        t = "object" if "properties" in s else "string"
    if t == "string":
        fmt = s.get("format", "")
        return {
            "date-time": "2024-01-01T00:00:00Z",
            "date": "2024-01-01",
            "email": "user@example.com",
            "uuid": "00000000-0000-4000-8000-000000000000",
            "uri": "https://example.com",
            "url": "https://example.com",
        }.get(fmt, f"{name or 'string'}-1".ljust(s.get("minLength", 0), "x"))
    if t == "integer":
        return max(1, int(s.get("minimum", 1)))
    if t == "number":
        return max(1.5, float(s.get("minimum", 1.5)))
    if t == "boolean":
        return True
    if t == "null":
        return None
    if t == "array":
        if depth > 4:
            return []
        return [fake(s.get("items", {}), spec, depth + 1, name)] * max(1, s.get("minItems", 1))
    if t == "object":
        if depth > 4:
            return {}
        props = s.get("properties", {})
        return {k: fake(v, spec, depth + 1, k) for k, v in props.items()}
    return None


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


def validate(value: Any, schema: dict | None, spec: dict | None = None, path: str = "$") -> list[str]:
    """Shallow-but-recursive validation: types, required, enums. Returns errors."""
    s = resolve(schema, spec)
    errors: list[str] = []
    if value is None:
        if s.get("nullable") or s.get("type") == "null" or (isinstance(s.get("type"), list) and "null" in s["type"]):
            return []
        return [f"{path}: must not be null"] if s else []
    if "enum" in s and value not in s["enum"]:
        errors.append(f"{path}: {value!r} not in {s['enum']}")
    t = s.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), None)
    if t in _TYPES:
        ok = isinstance(value, _TYPES[t]) and not (t in ("integer", "number") and isinstance(value, bool))
        if not ok:
            return errors + [f"{path}: expected {t}, got {type(value).__name__}"]
    if t == "object" or "properties" in s:
        if isinstance(value, dict):
            for req in s.get("required", []):
                if req not in value:
                    errors.append(f"{path}.{req}: required")
            for k, sub in s.get("properties", {}).items():
                if k in value:
                    errors += validate(value[k], sub, spec, f"{path}.{k}")
    if t == "array" and isinstance(value, list):
        for i, item in enumerate(value):
            errors += validate(item, s.get("items"), spec, f"{path}[{i}]")
    return errors


# -- mock server ------------------------------------------------------------

def _compile_routes(spec: dict) -> list[tuple[re.Pattern, dict, dict[str, str]]]:
    routes = []
    for path, item in spec.get("paths", {}).items():
        names: dict[str, str] = {}
        pattern = ""
        for i, chunk in enumerate(re.split(r"\{([^}]+)\}", path)):
            if i % 2:
                safe = f"p{len(names)}"
                names[safe] = chunk
                pattern += f"(?P<{safe}>[^/]+)"
            else:
                pattern += re.escape(chunk)
        routes.append((re.compile("^" + pattern + "$"), item, names))
    return routes


def success_response(op: dict) -> tuple[int, dict | None]:
    for code, resp in sorted(op.get("responses", {}).items()):
        if str(code).startswith("2"):
            content = (resp or {}).get("content", {})
            media = content.get("application/json") or next(iter(content.values()), None)
            return int(code), (media or {}).get("schema") if media else None
    return 200, None


def json_body_schema(op: dict, spec: dict | None = None) -> tuple[dict | None, bool]:
    body = resolve(op.get("requestBody"), spec) if op.get("requestBody") else None
    if not body:
        return None, False
    media = body.get("content", {}).get("application/json")
    return (media or {}).get("schema"), bool(body.get("required"))


class MockHandler(BaseHTTPRequestHandler):
    spec: dict = {}
    routes: list = []

    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, code: int, payload: Any) -> None:
        data = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(code)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method: str) -> None:
        url = urlparse(self.path)
        for rx, item, names in self.routes:
            m = rx.match(url.path)
            if not m:
                continue
            op = item.get(method)
            if op is None:
                return self._send(405, {"detail": "method not allowed"})
            params = {names[k]: v for k, v in m.groupdict().items()}
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            errors = []
            for p in item.get("parameters", []) + op.get("parameters", []):
                p = resolve(p, self.spec)
                if p.get("in") == "query" and p.get("required") and p["name"] not in query:
                    errors.append(f"query.{p['name']}: required")
                if p.get("in") == "path" and resolve(p.get("schema"), self.spec).get("type") == "integer":
                    if not re.fullmatch(r"-?\d+", params.get(p["name"], "")):
                        errors.append(f"path.{p['name']}: expected integer")
            schema, required = json_body_schema(op, self.spec)
            if schema is not None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if not raw:
                    if required:
                        errors.append("body: required")
                else:
                    try:
                        errors += validate(json.loads(raw), schema, self.spec, "body")
                    except json.JSONDecodeError:
                        errors.append("body: invalid JSON")
            if errors:
                return self._send(422, {"detail": errors})
            code, resp_schema = success_response(op)
            return self._send(code, None if resp_schema is None or code == 204 else fake(resp_schema, self.spec))
        self._send(404, {"detail": "not found"})

    def do_GET(self):
        self._handle("get")

    def do_POST(self):
        self._handle("post")

    def do_PUT(self):
        self._handle("put")

    def do_PATCH(self):
        self._handle("patch")

    def do_DELETE(self):
        self._handle("delete")


def start_mock(spec: dict | None = None, host: str = "127.0.0.1", port: int = 0):
    """Start the mock in a background thread. Returns (server, base_url)."""
    spec = spec if spec is not None else SPEC
    handler = type("BoundMockHandler", (MockHandler,), {"spec": spec, "routes": _compile_routes(spec)})
    server = ThreadingHTTPServer((host, port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{host}:{server.server_address[1]}"
