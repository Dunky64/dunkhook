"""Turn an error cluster into a candidate fix on its own branch, then a draft PR.

The agent gets read-only repo tools and submits exact search/replace edits. Edits
are applied, the test command (if any) is run, and failures go back to the agent
for another attempt. Every fix lands on ``triage/<cluster-id>`` and is opened as a
**draft** PR with ``gh`` when ``open_pr`` is set; otherwise the PR body is written
to ``.triage/<cluster-id>.md`` for a human to open.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..core.gitutil import Git
from ..core.llm import LLM
from ..core.proc import run
from ..core.repo_tools import list_tool, read_tool, safe_path, search_tool
from ..core.schema import array, enum, obj, string
from .cluster import Cluster

FIXER_SYSTEM = """You are an on-call engineer triaging a cluster of production errors. Investigate \
the repository with the tools, find the root cause, and propose the smallest safe fix. Edits are exact \
search/replace pairs: `old` must appear exactly once in the file, copied verbatim including indentation. \
Do not paper over the error with a bare `except`; fix the cause or validate input where the bad value \
enters. If you cannot find a credible fix, submit with no edits and explain what you learned. Add or \
extend a test when the repository has a test suite."""

SUBMIT_SCHEMA = obj(
    root_cause=string(),
    confidence=enum("low", "medium", "high"),
    edits=array(obj(path=string(), old=string("Exact existing text; empty string to create a new file"), new=string())),
    pr_title=string(),
    pr_body=string("Markdown: root cause, fix, risk, how it was verified"),
)


@dataclass
class FixOutcome:
    cluster_id: str
    status: str  # proposed | no_fix | tests_failed | edit_failed
    branch: str | None = None
    pr_url: str | None = None
    pr_body_path: str | None = None
    root_cause: str = ""
    confidence: str = ""


class EditError(ValueError):
    pass


def apply_edits(root: Path, edits: list[dict]) -> list[str]:
    """Apply all edits or none. Returns the touched paths."""
    staged: dict[Path, str] = {}
    for e in edits:
        path = safe_path(root, e["path"])
        current = staged.get(path)
        if current is None:
            current = path.read_text() if path.exists() else None
        if e["old"] == "":
            if current is not None:
                raise EditError(f"{e['path']} already exists; use a non-empty `old` to edit it")
            staged[path] = e["new"]
            continue
        if current is None:
            raise EditError(f"{e['path']} does not exist")
        n = current.count(e["old"])
        if n != 1:
            raise EditError(f"`old` text occurs {n} times in {e['path']} (must be exactly 1)")
        staged[path] = current.replace(e["old"], e["new"], 1)
    for path, text in staged.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return [str(p.relative_to(root)) for p in staged]


def _cluster_prompt(cluster: Cluster) -> str:
    s = cluster.summary()
    return (
        f"Error cluster `{cluster.id}`: {cluster.exc_type}, {cluster.count} occurrences "
        f"({s['first_seen']} → {s['last_seen']}).\n\nDistinct messages:\n"
        + "\n".join(f"- {m}" for m in s["messages"])
        + f"\n\nSample event:\n```\n{cluster.sample.raw[-6000:]}\n```\n"
    )


def propose_fix(
    repo: str | Path,
    cluster: Cluster,
    llm: LLM,
    test_cmd: str | None = None,
    attempts: int = 2,
    open_pr: bool = False,
    base_branch: str | None = None,
    log=print,
) -> FixOutcome:
    root = Path(repo).resolve()
    git = Git(root)
    dirty = [l for l in git.run("status", "--porcelain").splitlines() if not l.startswith("?? .triage/")]
    if dirty:
        raise RuntimeError("working tree is not clean; triage fixes need a clean checkout")
    start = git.current_branch()
    base = base_branch or start
    branch = f"triage/{cluster.id}"
    tools = [read_tool(root), search_tool(root), list_tool(root)]
    prompt = _cluster_prompt(cluster)
    outcome = FixOutcome(cluster.id, "no_fix")

    git.run("checkout", "-B", branch, base)
    try:
        for attempt in range(1, attempts + 1):
            log(f"[{cluster.id}] investigating (attempt {attempt})")
            fix = llm.agent(FIXER_SYSTEM, prompt, tools, SUBMIT_SCHEMA)
            outcome.root_cause, outcome.confidence = fix["root_cause"], fix["confidence"]
            if not fix["edits"]:
                outcome.status = "no_fix"
                break
            try:
                touched = apply_edits(root, fix["edits"])
            except (EditError, ValueError) as exc:
                outcome.status = "edit_failed"
                prompt += f"\n\nYour previous edits could not be applied: {exc}\nRe-read the files and try again."
                continue
            if test_cmd:
                res = run(test_cmd, cwd=root, timeout=1800)
                if not res.ok:
                    outcome.status = "tests_failed"
                    git.run("checkout", "--", ".")
                    git.run("clean", "-fdq", "-e", ".triage")
                    prompt += (
                        f"\n\nYour previous fix ({fix['pr_title']}) broke the tests:\n```\n{res.tail(5000)}\n```\n"
                        "Revise it."
                    )
                    continue
            body = fix["pr_body"] + _footer(cluster, test_cmd)
            git.commit_all(f"{fix['pr_title']}\n\nTriage cluster {cluster.id} ({cluster.count} events).", touched)
            outcome.status, outcome.branch = "proposed", branch
            if open_pr and shutil.which("gh"):
                git.run("push", "-u", "origin", branch)
                res = run(
                    ["gh", "pr", "create", "--draft", "--base", base, "--head", branch, "--title", fix["pr_title"], "--body", body],
                    cwd=root,
                )
                if res.ok:
                    outcome.pr_url = res.output.strip().splitlines()[-1]
                else:
                    log(f"[{cluster.id}] gh pr create failed: {res.tail(500)}")
            if outcome.pr_url is None:
                notes = root / ".triage"
                notes.mkdir(exist_ok=True)
                path = notes / f"{cluster.id}.md"
                path.write_text(f"# {fix['pr_title']}\n\nBranch: `{branch}`\n\n{body}\n")
                outcome.pr_body_path = str(path)
            break
    finally:
        if any(not l.startswith("?? .triage/") for l in git.run("status", "--porcelain").splitlines()):
            git.run("checkout", "--", ".", check=False)
            git.run("clean", "-fdq", "-e", ".triage", check=False)
        git.run("checkout", start)
        if outcome.status != "proposed":
            git.run("branch", "-D", branch, check=False)
    return outcome


def _footer(cluster: Cluster, test_cmd: str | None) -> str:
    verified = f"`{test_cmd}` passes on this branch." if test_cmd else "No test command was provided; not verified."
    summary = json.dumps(cluster.summary(), indent=2)
    return (
        f"\n\n---\n**Verification:** {verified}\n\n<details><summary>Error cluster</summary>\n\n"
        f"```json\n{summary}\n```\n</details>\n\n_Draft opened by dunkhook log-triage. Review before merging._\n"
    )
