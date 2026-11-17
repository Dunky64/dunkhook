"""dunkhook command line.

    dunkhook redteam   SPEC.md --out DIR [--rounds N]
    dunkhook depbot    REPO --test-cmd "pytest -q" [--commit]
    dunkhook triage    LOG... [--repo R --fix N --test-cmd ... --open-pr]
    dunkhook scaffold  openapi.json --out DIR [--edge-cases]
    dunkhook why       REPO path/to/file.py:42[-60] | --symbol NAME
    dunkhook migrate   plan|run|status REPO [...]

Every command that calls Claude accepts --model and --effort (defaults:
$DUNKHOOK_MODEL or claude-opus-5-5, $DUNKHOOK_EFFORT or high) and -v for a log of
model/tool calls on stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core.llm import DEFAULT_EFFORT, DEFAULT_MODEL, ClaudeLLM


def _llm(args) -> ClaudeLLM:
    return ClaudeLLM(model=args.model, effort=args.effort, verbose=args.verbose)


def _usage(llm) -> None:
    if isinstance(llm, ClaudeLLM):
        print(f"[tokens] in={llm.usage['input_tokens']:,} out={llm.usage['output_tokens']:,}", file=sys.stderr)


def _emit(text: str, out: str | None) -> None:
    if out:
        Path(out).write_text(text)
        print(f"wrote {out}")
    else:
        print(text)


def cmd_redteam(args) -> int:
    from .redteam import run_redteam

    llm = _llm(args)
    initial = Path(args.initial).read_text() if args.initial else None
    res = run_redteam(Path(args.spec).read_text(), llm, args.out, rounds=args.rounds, fix_attempts=args.fix_attempts,
                      initial_code=initial, timeout=args.timeout)
    print(f"\nsurvivor: {res.out_dir / 'solution.py'}  accepted attack files: {res.accepted_tests}  open breaks: {res.open_breaks}")
    print(f"report:   {res.out_dir / 'report.md'}")
    _usage(llm)
    return 0 if res.survived_all else 1


def cmd_depbot(args) -> int:
    from .depbot import DepBot, render_report

    llm = None if args.no_llm else _llm(args)
    bot = DepBot(args.repo, args.test_cmd, requirements=args.requirements, python=args.python or sys.executable,
                 install_cmd=args.install_cmd, llm=llm, max_attempts=args.max_attempts, commit=args.commit,
                 include_prereleases=args.pre)
    results = bot.run(only=args.only)
    _emit(render_report(results), args.report)
    _usage(llm)
    return 0


def cmd_triage(args) -> int:
    from .logtriage import cluster_events, parse_files, propose_fix, render_clusters

    clusters = cluster_events(parse_files(args.logs), repo_markers=args.app_path or [])
    if args.json:
        _emit(json.dumps([c.summary() for c in clusters], indent=2), args.report)
    else:
        _emit(render_clusters(clusters), args.report)
    if args.fix:
        if not args.repo:
            print("--fix needs --repo", file=sys.stderr)
            return 2
        llm = _llm(args)
        for c in clusters[: args.fix]:
            outcome = propose_fix(args.repo, c, llm, test_cmd=args.test_cmd, open_pr=args.open_pr, base_branch=args.base)
            where = outcome.pr_url or outcome.pr_body_path or ""
            print(f"[{c.id}] {outcome.status} {outcome.branch or ''} {where}".rstrip())
        _usage(llm)
    return 0


def cmd_scaffold(args) -> int:
    from .scaffold import load_spec, scaffold

    llm = _llm(args) if args.edge_cases else None
    res = scaffold(load_spec(args.spec), args.out, package=args.package, llm=llm, verify=not args.no_verify)
    if not res.tests_passed:
        print(res.test_output[-3000:])
    _usage(llm)
    return 0 if res.tests_passed else 1


def cmd_why(args) -> int:
    from .archaeologist import investigate, render

    if not args.target and not args.symbol:
        print("give a target (file.py:42 or file.py:40-55) or --symbol NAME", file=sys.stderr)
        return 2
    llm = _llm(args)
    result = investigate(args.repo, llm, target=args.target, symbol=args.symbol, question=args.question)
    _emit(json.dumps(result, indent=2) if args.json else render(result), args.out)
    _usage(llm)
    return 0


def cmd_migrate(args) -> int:
    from .migrate import Checklist, migrate, plan

    if args.action == "plan":
        cl = plan(args.repo)
        print(cl.render())
        return 0
    if args.action == "status":
        print(Checklist.load(Path(args.repo).resolve()).render())
        return 0
    llm = _llm(args)
    cl = migrate(args.repo, llm, max_files=args.max_files, max_attempts=args.max_attempts, verify_cmd=args.verify_cmd,
                 test_cmd=args.test_cmd, commit=args.commit)
    print(cl.render())
    _usage(llm)
    return 0 if all(i.status == "done" for i in cl.items) else 1


def cmd_coach(args) -> int:
    import re

    from .cs2coach import Coach, install_cfg, make_server

    if args.action == "install-cfg":
        path, _ = install_cfg(args.cfg_dir, args.port)
        print(f"wrote {path}\nRestart CS2, then run:\n  dunkhook coach run --cfg {path}")
        return 0
    m = re.search(r'"token"\s+"([^"]+)"', Path(args.cfg).read_text())
    if not m:
        print(f"no auth token in {args.cfg}", file=sys.stderr)
        return 2
    llm = None if args.no_ai else _llm(args)
    coach = Coach(m.group(1), llm=llm)
    server = make_server(coach, args.port)
    print(f"coach listening on http://127.0.0.1:{args.port}  (open it in a browser on your second screen)")
    print("AI tips: " + ("off" if llm is None else f"{args.model}, effort {args.effort}, once per round"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    _usage(llm)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dunkhook", description="Agentic, loop-driven dev tools powered by Claude.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--model", default=DEFAULT_MODEL)
    common.add_argument("--effort", default=DEFAULT_EFFORT, choices=["low", "medium", "high", "xhigh", "max"])
    common.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("redteam", parents=[common], help="builder vs breaker loop; keep what survives")
    s.add_argument("spec", help="text/markdown file describing the module to build")
    s.add_argument("--out", required=True)
    s.add_argument("--rounds", type=int, default=5)
    s.add_argument("--fix-attempts", type=int, default=3)
    s.add_argument("--initial", help="start from an existing solution.py instead of generating one")
    s.add_argument("--timeout", type=float, default=60, help="seconds per pytest run")
    s.set_defaults(fn=cmd_redteam)

    s = sub.add_parser("depbot", parents=[common], help="bump pins one by one, test, revert on failure")
    s.add_argument("repo")
    s.add_argument("--test-cmd", required=True)
    s.add_argument("--requirements", default="requirements.txt")
    s.add_argument("--python", help="interpreter of the project's virtualenv (default: this one)")
    s.add_argument("--install-cmd", help="default: '{python} -m pip install -q -r {req}'")
    s.add_argument("--only", nargs="*", help="limit to these packages")
    s.add_argument("--max-attempts", type=int, default=3)
    s.add_argument("--pre", action="store_true", help="consider pre-releases")
    s.add_argument("--commit", action="store_true", help="commit each successful bump")
    s.add_argument("--no-llm", action="store_true", help="heuristic fallback instead of Claude's diagnosis")
    s.add_argument("--report", help="write the markdown report here")
    s.set_defaults(fn=cmd_depbot)

    s = sub.add_parser("triage", parents=[common], help="cluster errors in logs; optionally draft fixes")
    s.add_argument("logs", nargs="+")
    s.add_argument("--repo", help="repository the logs come from")
    s.add_argument("--app-path", action="append", help="substring marking application frames (repeatable)")
    s.add_argument("--fix", type=int, default=0, help="draft fixes for the top N clusters")
    s.add_argument("--test-cmd")
    s.add_argument("--open-pr", action="store_true", help="push and open draft PRs with gh")
    s.add_argument("--base", help="base branch for fix branches/PRs (default: current)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--report")
    s.set_defaults(fn=cmd_triage)

    s = sub.add_parser("scaffold", parents=[common], help="OpenAPI → typed client, mock server, tests")
    s.add_argument("spec")
    s.add_argument("--out", required=True)
    s.add_argument("--package")
    s.add_argument("--edge-cases", action="store_true", help="have Claude add verified edge-case tests")
    s.add_argument("--no-verify", action="store_true")
    s.set_defaults(fn=cmd_scaffold)

    s = sub.add_parser("why", parents=[common], help="explain why code exists from git/GitHub history")
    s.add_argument("repo")
    s.add_argument("target", nargs="?", help="path/to/file.py:42 or path/to/file.py:40-55")
    s.add_argument("--symbol")
    s.add_argument("--question", default="Why is this here?")
    s.add_argument("--json", action="store_true")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_why)

    s = sub.add_parser("migrate", parents=[common], help="Flask → FastAPI, file by file, self-verified")
    s.add_argument("action", choices=["plan", "run", "status"])
    s.add_argument("repo")
    s.add_argument("--max-files", type=int)
    s.add_argument("--max-attempts", type=int, default=3)
    s.add_argument("--verify-cmd", help="per-file check, e.g. 'python -c \"import ast,sys; ast.parse(open({file}).read())\"'")
    s.add_argument("--test-cmd", help="full suite, run once every item is processed")
    s.add_argument("--commit", action="store_true")
    s.set_defaults(fn=cmd_migrate)

    s = sub.add_parser("coach", help="CS2 live coach overlay (Game State Integration + Claude)")
    cs = s.add_subparsers(dest="action", required=True)
    c = cs.add_parser("install-cfg", help="write the GSI cfg into CS2's cfg folder")
    c.add_argument("cfg_dir", help=r"e.g. ...\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg")
    c.add_argument("--port", type=int, default=3000)
    c = cs.add_parser("run", help="run the coach server and overlay")
    c.add_argument("--model", default=DEFAULT_MODEL)
    # Freezetime is ~20s, so the coach defaults to low effort: a quick tip beats a deep one.
    c.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"])
    c.add_argument("-v", "--verbose", action="store_true")
    c.add_argument("--cfg", required=True, help="the gamestate_integration_dunkhook.cfg written by install-cfg")
    c.add_argument("--port", type=int, default=3000)
    c.add_argument("--no-ai", action="store_true", help="rules only, no Claude calls")
    s.set_defaults(fn=cmd_coach)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
