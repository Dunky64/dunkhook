"""Codebase archaeologist: answers "why is this here?" from git history and GitHub.

A deterministic pre-dig runs ``git blame`` on the target lines and collects the
commits and ``#123`` references it finds. Claude then digs further with tools
(line history, commit bodies, pickaxe search, issues and PRs) and submits an
answer that cites its evidence.

GitHub lookups need ``GITHUB_TOKEN`` (or ``GH_TOKEN``) and an ``origin`` remote on
github.com. Without them the agent works from git alone and says so.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..core.gitutil import Git, GitError
from ..core.llm import LLM, Tool
from ..core.repo_tools import safe_path
from ..core.schema import array, enum, integer, obj, string

REF_RE = re.compile(r"(?:(?:[Ff]ix(?:es|ed)?|[Cc]lose[sd]?|[Rr]esolve[sd]?|[Rr]efs?|PR|pull request)\s*)?#(\d+)\b")

SYSTEM = """You are a codebase archaeologist. Explain WHY a piece of code exists: the problem it \
solved, the decision behind it, and whether that reason still holds. Work from evidence: blame, line \
history, commit messages, linked issues and PRs. Follow the trail past refactors and renames to the \
commit that introduced the behaviour, not just the last one that touched the line. Distinguish what \
the evidence shows from what you infer, and say plainly when the trail runs cold."""

ANSWER_SCHEMA = obj(
    answer=string("Markdown explanation: what it does, why it was added, whether the reason still applies"),
    confidence=enum("low", "medium", "high"),
    origin_commit=string("SHA of the commit that introduced the behaviour, or empty if unknown"),
    evidence=array(obj(kind=enum("commit", "issue", "pr", "code", "other"), ref=string(), note=string())),
    still_needed=enum("yes", "no", "unclear"),
    open_questions=array(string()),
)


@dataclass
class Target:
    path: str
    start: int
    end: int

    @classmethod
    def parse(cls, spec: str) -> "Target":
        m = re.fullmatch(r"(.+?):(\d+)(?:-(\d+))?", spec)
        if not m:
            raise ValueError("target must look like path/to/file.py:42 or path/to/file.py:40-55")
        start = int(m.group(2))
        return cls(m.group(1), start, int(m.group(3) or start))


def find_symbol(git: Git, symbol: str) -> Target:
    """Locate a definition with git grep (def/class/function/const/assignment)."""
    pattern = rf"(def|class|function|const|let|var|func|fn)\s+{re.escape(symbol)}\b|^\s*{re.escape(symbol)}\s*="
    out = git.run("grep", "-n", "-E", pattern, check=False).strip()
    if not out:
        raise ValueError(f"could not find a definition of {symbol!r}")
    path, line, _ = out.splitlines()[0].split(":", 2)
    return Target(path, int(line), int(line))


class GitHub:
    def __init__(self, slug: str | None, token: str | None):
        self.slug, self.token = slug, token

    @property
    def available(self) -> bool:
        return bool(self.slug and self.token)

    def get(self, path: str):
        if not self.available:
            raise RuntimeError("GitHub lookups unavailable (need GITHUB_TOKEN and a github.com origin remote)")
        req = urllib.request.Request(
            f"https://api.github.com/repos/{self.slug}{path}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "dunkhook-archaeologist",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"GitHub API {e.code} for {path}") from None


def blame_summary(git: Git, t: Target) -> tuple[str, list[str]]:
    out = git.run("blame", "--porcelain", "-L", f"{t.start},{t.end}", "--", t.path)
    commits: dict[str, dict] = {}
    order: list[str] = []
    lines: list[tuple[str, str]] = []
    current = None
    for line in out.splitlines():
        m = re.match(r"^([0-9a-f]{40}) \d+ \d+", line)
        if m:
            current = m.group(1)
            if current not in commits:
                commits[current] = {}
                order.append(current)
        elif line.startswith("\t") and current:
            lines.append((current, line[1:]))
        elif current and " " in line:
            key, val = line.split(" ", 1)
            commits[current].setdefault(key, val)
    text = []
    for sha in order:
        c = commits[sha]
        text.append(f"{sha[:10]}  {c.get('author', '?')}  {_date(c.get('author-time'))}  {c.get('summary', '')}")
    text.append("")
    text += [f"{sha[:10]} | {src}" for sha, src in lines]
    return "\n".join(text), [s for s in order if not s.startswith("0000000")]


def _date(ts: str | None) -> str:
    if not ts:
        return "?"
    import datetime

    return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%Y-%m-%d")


def make_tools(git: Git, gh: GitHub) -> list[Tool]:
    root = git.root

    def blame(a):
        return blame_summary(git, Target(a["path"], a["start_line"], a["end_line"]))[0]

    def line_history(a):
        n = max(1, min(a["max_commits"] or 8, 30))
        return git.run(
            "log", f"-n{n}", "--format=%n=== %H %ad %an%n%s%n%b", "--date=short",
            "-L", f"{a['start_line']},{a['end_line']}:{a['path']}",
        )

    def show_commit(a):
        return git.run("show", "--stat", "--patch", "--format=fuller", a["sha"])

    def pickaxe(a):
        out = git.run("log", "-S", a["text"], "--format=%h %ad %an  %s", "--date=short", "--", *( [a["path"]] if a["path"] else [] ))
        return out or "no commits added or removed that text"

    def grep_log(a):
        return git.run("log", "-E", "-i", f"--grep={a['pattern']}", "-n50", "--format=%h %ad %an  %s", "--date=short") or "no matches"

    def read_at(a):
        rev = a["rev"] or "HEAD"
        safe_path(root, a["path"])
        text = git.run("show", f"{rev}:{a['path']}")
        lines = text.splitlines()
        s, e = max(1, a["start_line"] or 1), min(len(lines), a["end_line"] or len(lines))
        return "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(s, e + 1))

    def issue(a):
        data = gh.get(f"/issues/{a['number']}")
        kind = "PR" if "pull_request" in data else "Issue"
        parts = [f"{kind} #{data['number']}: {data['title']} [{data['state']}] by {data['user']['login']} {data['created_at'][:10]}", "", data.get("body") or "(no body)"]
        if data.get("comments"):
            for c in gh.get(f"/issues/{a['number']}/comments?per_page=10"):
                parts += ["", f"--- {c['user']['login']} {c['created_at'][:10]}:", c.get("body") or ""]
        return "\n".join(parts)

    def prs_for_commit(a):
        data = gh.get(f"/commits/{a['sha']}/pulls")
        return "\n".join(f"#{p['number']} {p['title']} [{p['state']}]" for p in data) or "no pull requests found for that commit"

    tools = [
        Tool("blame", "git blame for a line range at HEAD: who/when/which commit last touched each line.",
             obj(path=string(), start_line=integer(), end_line=integer()), blame),
        Tool("line_history", "Full history of a line range (git log -L), following it through edits. Shows diffs.",
             obj(path=string(), start_line=integer(), end_line=integer(), max_commits=integer()), line_history),
        Tool("show_commit", "Show a commit: full message, stats and patch.", obj(sha=string()), show_commit),
        Tool("pickaxe", "Commits that added or removed an exact string (git log -S). path may be '' for the whole repo.",
             obj(text=string(), path=string()), pickaxe),
        Tool("grep_log", "Search commit messages with a regex (case-insensitive).", obj(pattern=string()), grep_log),
        Tool("read_file_at", "Read a file at a revision (rev '' = HEAD). 0 for start/end means whole file.",
             obj(path=string(), rev=string(), start_line=integer(), end_line=integer()), read_at),
    ]
    if gh.available:
        tools += [
            Tool("github_issue", "Fetch a GitHub issue or PR by number, with its first comments.", obj(number=integer()), issue),
            Tool("prs_for_commit", "List GitHub pull requests that contain a commit.", obj(sha=string()), prs_for_commit),
        ]
    return tools


def investigate(
    repo: str | Path,
    llm: LLM,
    target: str | None = None,
    symbol: str | None = None,
    question: str = "Why is this here?",
    token: str | None = None,
    max_turns: int = 25,
) -> dict:
    git = Git(repo)
    if not git.is_repo():
        raise GitError(f"{repo} is not a git repository")
    t = Target.parse(target) if target else find_symbol(git, symbol or "")
    gh = GitHub(git.github_slug(), token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))

    lines = git.run("show", f"HEAD:{t.path}").splitlines()
    ctx_start, ctx_end = max(1, t.start - 5), min(len(lines), t.end + 5)
    snippet = "\n".join(f"{'>' if t.start <= i <= t.end else ' '}{i:>5}  {lines[i - 1]}" for i in range(ctx_start, ctx_end + 1))
    blame_text, shas = blame_summary(git, t)
    refs = set()
    for sha in shas:
        refs.update(REF_RE.findall(git.run("log", "-1", "--format=%B", sha)))

    prompt = (
        f"Question: {question}\n\nTarget: {t.path}:{t.start}-{t.end}\n\n```\n{snippet}\n```\n\n"
        f"Blame:\n```\n{blame_text}\n```\n\n"
        f"Issue/PR references in those commits: {', '.join('#' + r for r in sorted(refs, key=int)) or 'none'}\n"
        f"GitHub lookups: {'available' if gh.available else 'NOT available (git history only)'}\n"
    )
    result = llm.agent(SYSTEM, prompt, make_tools(git, gh), ANSWER_SCHEMA, max_turns=max_turns)
    result["target"] = f"{t.path}:{t.start}-{t.end}"
    return result


def render(result: dict) -> str:
    lines = [f"# Why is `{result['target']}` here?", "", result["answer"], "",
             f"**Confidence:** {result['confidence']} · **Still needed:** {result['still_needed']}"]
    if result.get("origin_commit"):
        lines.append(f"**Introduced in:** `{result['origin_commit']}`")
    if result["evidence"]:
        lines += ["", "## Evidence", ""]
        lines += [f"- [{e['kind']}] `{e['ref']}`: {e['note']}" for e in result["evidence"]]
    if result["open_questions"]:
        lines += ["", "## Open questions", ""] + [f"- {q}" for q in result["open_questions"]]
    return "\n".join(lines) + "\n"
