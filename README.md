# dunkhook

Agentic, loop-driven dev tools built on Claude, plus a CS2 live coach. Each one is a loop: the model proposes something, deterministic checks (tests, AST diffs, git) decide whether it stands, and failures are fed back until it passes or the tool gives up cleanly.

| Command | What it does | What decides "done" |
|---|---|---|
| `dunkhook redteam` | One agent writes code, another tries to break it; you keep what survives | pytest, plus a judge that rules on disputed tests |
| `dunkhook depbot` | Bumps pins one at a time, runs the suite, reverts on failure | your test command |
| `dunkhook triage` | Clusters production errors and drafts PRs with candidate fixes | your test command on a `triage/<id>` branch |
| `dunkhook scaffold` | OpenAPI → typed client, mock server, integration tests | the generated tests against the generated mock |
| `dunkhook why` | Answers "why is this here?" from blame, history, issues and PRs | evidence the agent has to cite |
| `dunkhook migrate` | Flask → FastAPI, file by file, with a resumable checklist | compile check, no flask imports left, route parity |

## Install

```bash
pip install -e .            # Python 3.10+
pip install -e '.[yaml]'    # if your OpenAPI specs are YAML
export ANTHROPIC_API_KEY=... # or `ant auth login`
```

Defaults: model `claude-opus-5-5` with adaptive thinking at effort `high`. Override with `--model` / `--effort` or `DUNKHOOK_MODEL` / `DUNKHOOK_EFFORT`. Requests enable server-side refusal fallbacks (`fallbacks: "default"`), so a declined request is retried on a suitable model inside the same call. `-v` logs every model and tool call to stderr, and token totals print when a command finishes.

## The tools

### 1. Red-team harness

```bash
dunkhook redteam examples/redteam_spec.md --out out/duration --rounds 6
```

Each round:

1. The **breaker** writes a new pytest file aimed at the current `solution.py`. A file that doesn't collect is thrown out and the breaker is told why.
2. If every test passes, the solution survived the round.
3. Otherwise the **builder** fixes the code, or disputes tests that go beyond the spec. A separate **judge** rules on each dispute, and invalid tests are deselected for good.
4. The fix has to pass the *whole* accepted suite, including earlier rounds. If the builder can't get there, the code rolls back to the last survivor and the unbeaten tests go to `open_breaks/`.

Output: the surviving `solution.py`, the accepted `tests/`, `open_breaks/`, `struck_tests.json` and `report.md`. The exit code is non-zero if any break is still open. Model-written code runs locally, so use a container if that matters to you.

### 2. Dependency-upgrade bot

```bash
dunkhook depbot path/to/project --test-cmd "pytest -q" --python path/to/project/.venv/bin/python --commit --report deps.md
```

- The baseline suite must pass first. Otherwise failures can't be blamed on an upgrade.
- For each `name==version` pin, the bot tries the newest release. On failure, Claude classifies the failure (`breaking_api`, `dependency_conflict`, `flaky_test`, …), says how to adopt the new version, and picks the next candidate to try (for example, the last release before a breaking major).
- Bumps that never go green are reverted in both the file and the environment. Green bumps are committed one per package.
- `--no-llm` falls back to stepping down a major, then a minor. Versions come from `pip index versions`. `StaticSource` and the `VersionSource` protocol let you plug in another source.

### 3. Log-triage agent

```bash
dunkhook triage logs/*.log                                  # cluster report only, no model calls
dunkhook triage logs/*.log --repo . --fix 3 --test-cmd "pytest -q" --open-pr
```

- It parses Python tracebacks (including chained ones), JSON log lines and plain `ERROR` lines.
- Each event gets a fingerprint: exception type plus the top application frames. Messages are normalised, so ids, numbers, UUIDs and timestamps don't split clusters. Frame-less errors with near-identical messages are merged.
- For the top N clusters, an agent with read-only repo tools finds the root cause and submits exact search/replace edits. The edits are applied all-or-nothing and the tests run. On failure, the agent gets the test output and tries again.
- Each fix is committed to `triage/<cluster-id>`. With `--open-pr`, the branch is pushed and a **draft** PR is opened with `gh`. Without it, the PR body is written to `.triage/<id>.md`. Your checked-out branch is never modified.
- Use `--app-path /srv/app` to say which frames belong to your code.

### 4. Spec-to-scaffold pipeline

```bash
dunkhook scaffold examples/petstore.json --out out/petstore              # deterministic
dunkhook scaffold examples/petstore.json --out out/petstore --edge-cases # + Claude-written edge-case tests
python -m petstore_client.mock_server 8080                               # run the mock (from out/petstore)
```

Code generation is deterministic and uses only the standard library:

- dataclass models with `from_dict` / `to_dict`
- one typed client method per operation
- a mock server driven by the embedded spec: example data, 422 on invalid input, 404/405
- pytest integration tests, including negative cases

The pipeline runs the generated tests to verify the output. With `--edge-cases`, Claude writes extra tests in a generate → run → fix loop. If the file still fails after 3 rounds, it is deleted, so only passing tests are kept.

### 5. Codebase archaeologist

```bash
dunkhook why . src/retry.py:40-52
dunkhook why . --symbol RATE_LIMIT_SLEEP --question "Can we delete this?"
```

A deterministic pre-pass runs `git blame` and collects `#123` references. The agent then digs with these tools:

- `line_history` (`git log -L`)
- `show_commit`
- `pickaxe` (`git log -S`)
- `grep_log`
- `read_file_at` (any revision)

With `GITHUB_TOKEN` set, it also gets `github_issue` and `prs_for_commit`. The answer separates evidence from inference. It names the commit that introduced the behaviour, says whether the reason still holds, and lists open questions.

### 6. Migration assistant (Flask → FastAPI)

```bash
dunkhook migrate plan  examples/flask_app     # writes .migration/checklist.json + CHECKLIST.md
dunkhook migrate run   examples/flask_app --commit --test-cmd "pytest -q"
dunkhook migrate status examples/flask_app
```

Helpers are migrated first, then blueprints, then the module that creates the app. Every converted file must pass these checks:

- it compiles
- no `flask` imports are left
- route parity: the same `(METHOD, path)` set and router prefixes as before, compared on the AST, so `<int:id>` and `{id}` are treated as equal
- an optional `--verify-cmd` (with `{file}` substituted)

Problems go back to Claude, up to `--max-attempts` times. After that, the original file is restored and the item is marked failed. Progress is saved after every file, so `run` resumes where it stopped. The full test suite runs once, after every item has been processed.

### 7. CS2 live coach overlay

```bash
dunkhook coach install-cfg "C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg"
dunkhook coach run --cfg ".../game/csgo/cfg/gamestate_integration_dunkhook.cfg"   # add --no-ai for rules only
```

Restart CS2, then open http://127.0.0.1:3000 on a second screen (or a phone pointed at the PC). It reads CS2's official **Game State Integration** feed. The generated cfg asks only for your own player, the map and the round. It does not ask for enemy positions, the bomb's location or grenades, and GSI doesn't give those to a player anyway. When you're dead, GSI sends the teammate you're spectating, and the coach ignores that data.

- **Rules (instant):** the buy call (full buy, save, eco, or a forced buy on the last round of a half or at the enemy's match point) comes from your money and your side's loss bonus. During the round you get a client-side C4 clock with the latest defuse start time (kit or no kit), a low-HP warning, and a reload reminder.
- **Claude (once per round, at the start of the buy phase):** it reads your last few rounds (kills, deaths, money, equipment value, wins) and gives one focus for the round, under 20 words, plus the pattern it's based on. It runs in the background at effort `low` so it fits inside the buy phase. If the call fails, the rules keep working.

It reads only data Valve provides to third-party tools. It doesn't touch the game's memory, files or input. Some leagues and tournaments (FACEIT, ESEA, LAN events) ban live outside help during matches, so check their rules before using it there.

## Layout

```
dunkhook/core/       LLM wrapper (structured JSON + manual tool loop), strict-schema helpers, git/proc/repo tools
dunkhook/redteam/    builder / breaker / judge loop
dunkhook/depbot/     pin parsing, version sources, bump-test-revert loop
dunkhook/logtriage/  parsing + clustering, fixer agent + draft PRs
dunkhook/scaffold/   OpenAPI codegen + runtime (mock, fake data, validator)
dunkhook/archaeologist/  git/GitHub tools + investigation agent
dunkhook/migrate/    AST route extraction, checklist, per-file migrate-verify loop
dunkhook/cs2coach/   CS2 Game State Integration server, coaching rules, overlay, per-round AI tip
```

All model access goes through two primitives in `dunkhook/core/llm.py`:

- `LLM.json(system, prompt, schema)`: structured outputs
- `LLM.agent(system, prompt, tools, submit_schema)`: a tool loop that ends when the model calls `submit`

To reuse the loops in your own scripts, use the Python API (`run_redteam`, `DepBot`, `propose_fix`, `scaffold`, `investigate`, `migrate`). Each accepts any object with those two methods.

## Tests

```bash
pytest -q
```

The suite drives every loop end to end with `ScriptedLLM`, a deterministic stand-in for Claude, and makes no API calls. It covers:

- real git repos and real pytest runs
- a generated client talking to its generated mock over HTTP
- the `ClaudeLLM` request and tool-loop logic, run against a fake Anthropic client
