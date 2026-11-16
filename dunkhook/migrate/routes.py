"""Static route extraction for Flask and FastAPI modules (AST, no imports executed).

Used as the migration's self-verification: the set of (METHOD, path) pairs a file
declares must be identical before and after conversion, and router prefixes
(``Blueprint(url_prefix=...)`` vs ``APIRouter(prefix=...)``) must match too.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

HTTP = {"get", "post", "put", "patch", "delete", "head", "options"}


def normalise_path(path: str) -> str:
    """'/u/<int:id>' and '/u/{id}' both become '/u/{}'; '{p:path}' / '<path:p>' become '{*}'."""
    path = re.sub(r"<path:[^>]+>", "{*}", path)
    path = re.sub(r"\{[^}:]+:path\}", "{*}", path)
    path = re.sub(r"<(?:[^:>]+:)?[^>]+>", "{}", path)
    path = re.sub(r"\{[^}*]+\}", "{}", path)
    return path


@dataclass
class RouteTable:
    routes: set[tuple[str, str]] = field(default_factory=set)
    prefixes: dict[str, str] = field(default_factory=dict)  # router variable -> prefix
    imports_flask: bool = False
    imports_fastapi: bool = False

    def describe(self) -> list[str]:
        return sorted(f"{m} {p}" for m, p in self.routes)


def _str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _methods(node: ast.AST | None) -> list[str] | None:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        vals = [_str(e) for e in node.elts]
        if all(vals):
            return [v.upper() for v in vals]  # type: ignore[union-attr]
    return None


def _kw(call: ast.Call, name: str) -> ast.AST | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def extract(source: str) -> RouteTable:
    tree = ast.parse(source)
    table = RouteTable()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                table.imports_flask |= a.name.split(".")[0] == "flask" or a.name.startswith("flask_")
                table.imports_fastapi |= a.name.split(".")[0] in ("fastapi", "starlette")
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            table.imports_flask |= root == "flask" or root.startswith("flask_")
            table.imports_fastapi |= root in ("fastapi", "starlette")

        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and len(node.targets) == 1:
            target = node.targets[0]
            func = node.value.func
            fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if isinstance(target, ast.Name):
                if fname == "Blueprint":
                    table.prefixes[target.id] = _str(_kw(node.value, "url_prefix")) or ""
                elif fname == "APIRouter":
                    table.prefixes[target.id] = _str(_kw(node.value, "prefix")) or ""

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                attr = dec.func.attr
                path = _str(dec.args[0]) if dec.args else _str(_kw(dec, "path")) or _str(_kw(dec, "rule"))
                if path is None:
                    continue
                if attr == "route":  # Flask
                    for m in _methods(_kw(dec, "methods")) or ["GET"]:
                        table.routes.add((m, normalise_path(path)))
                elif attr == "api_route":  # FastAPI
                    for m in _methods(_kw(dec, "methods")) or ["GET"]:
                        table.routes.add((m, normalise_path(path)))
                elif attr in HTTP:  # Flask 2 shortcuts and FastAPI
                    table.routes.add((attr.upper(), normalise_path(path)))

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("add_url_rule", "add_api_route"):
            path = _str(node.args[0]) if node.args else _str(_kw(node, "rule")) or _str(_kw(node, "path"))
            if path is not None:
                for m in _methods(_kw(node, "methods")) or ["GET"]:
                    table.routes.add((m, normalise_path(path)))
    return table


def compare(before: RouteTable, after: RouteTable) -> list[str]:
    problems = []
    missing = before.routes - after.routes
    extra = after.routes - before.routes
    if missing:
        problems.append("routes missing after migration: " + ", ".join(f"{m} {p}" for m, p in sorted(missing)))
    if extra:
        problems.append("routes added that were not in the original: " + ", ".join(f"{m} {p}" for m, p in sorted(extra)))
    if sorted(before.prefixes.values()) != sorted(after.prefixes.values()):
        problems.append(f"router prefixes differ: before {sorted(before.prefixes.values())}, after {sorted(after.prefixes.values())}")
    return problems
