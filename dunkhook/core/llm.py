"""The one place dunkhook talks to Claude.

Every project uses two primitives:

* ``LLM.json``  - one request, structured-output JSON back (validated by the API
  against a JSON schema).
* ``LLM.agent`` - a manual tool-use loop: Claude calls the tools you hand it until
  it calls the built-in ``submit`` tool, whose input is returned to you.

``ClaudeLLM`` is the real implementation. ``ScriptedLLM`` is a deterministic
stand-in used by the test-suite and by ``--dry-run`` style experiments.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

DEFAULT_MODEL = os.environ.get("DUNKHOOK_MODEL", "claude-opus-5-5")
DEFAULT_EFFORT = os.environ.get("DUNKHOOK_EFFORT", "high")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOOL_OUTPUT = 24_000


class LLMError(RuntimeError):
    """The model could not produce a usable answer."""


class LLMRefusal(LLMError):
    """The model (and its server-side fallback) declined the request."""


@dataclass
class Tool:
    """A client-side tool the agent loop can call."""

    name: str
    description: str
    input_schema: dict
    fn: Callable[[dict], str]

    def spec(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "strict": True,
        }


class LLM(Protocol):
    def json(self, system: str, prompt: str, schema: dict) -> dict: ...

    def agent(
        self,
        system: str,
        prompt: str,
        tools: list[Tool],
        submit_schema: dict,
        max_turns: int = 30,
    ) -> dict: ...


def _clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return (
        text[:limit]
        + f"\n\n[output truncated: {len(text) - limit} more characters. "
        "Narrow the request (smaller line range, more specific pattern) to see the rest.]"
    )


def run_tool(tool: Tool, args: dict) -> tuple[str, bool]:
    """Run a tool, returning (output, is_error). Never raises."""
    try:
        return _clip(str(tool.fn(args))), False
    except Exception as exc:  # tool errors go back to the model, not up the stack
        return f"Error: {type(exc).__name__}: {exc}", True


class ClaudeLLM:
    """Claude via the official Anthropic SDK.

    Uses adaptive thinking (always on for Opus 5.5), an explicit effort level, and
    server-side refusal fallbacks (``fallbacks="default"``) so a safety-classifier
    decline is retried on a suitable model inside the same call.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        client: Any = None,
        verbose: bool = False,
    ):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.effort = effort
        self.verbose = verbose
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    # -- helpers ---------------------------------------------------------
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[llm] {msg}", file=sys.stderr)

    def _track(self, msg: Any) -> None:
        usage = getattr(msg, "usage", None)
        if usage is not None:
            self.usage["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
            self.usage["output_tokens"] += getattr(usage, "output_tokens", 0) or 0

    @staticmethod
    def _check(msg: Any) -> None:
        if msg.stop_reason == "refusal":
            details = getattr(msg, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMRefusal(f"model declined the request (category={category})")
        if msg.stop_reason == "max_tokens":
            raise LLMError("response hit max_tokens before finishing")

    # -- primitives ------------------------------------------------------
    def json(self, system: str, prompt: str, schema: dict) -> dict:
        self._log(f"json request ({len(prompt)} chars)")
        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=64000,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
        self._track(msg)
        self._check(msg)
        text = "".join(b.text for b in msg.content if b.type == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned invalid JSON: {exc}") from exc

    def agent(
        self,
        system: str,
        prompt: str,
        tools: list[Tool],
        submit_schema: dict,
        max_turns: int = 30,
    ) -> dict:
        by_name = {t.name: t for t in tools}
        specs = [t.spec() for t in tools] + [
            {
                "name": "submit",
                "description": "Submit your final answer. Call this exactly once, when you are done investigating.",
                "input_schema": submit_schema,
                "strict": True,
            }
        ]
        messages: list[dict] = [{"role": "user", "content": prompt}]
        for turn in range(max_turns):
            self._log(f"agent turn {turn + 1}/{max_turns}")
            msg = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                tools=specs,
                messages=messages,
                output_config={"effort": self.effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
            self._track(msg)
            self._check(msg)
            # Append the full content (thinking blocks included) unchanged.
            messages.append({"role": "assistant", "content": msg.content})
            if msg.stop_reason == "pause_turn":
                continue
            uses = [b for b in msg.content if b.type == "tool_use"]
            if not uses:
                messages.append(
                    {"role": "user", "content": "Continue. When finished, call the submit tool with your final answer."}
                )
                continue
            results = []
            submitted = None
            for use in uses:
                if use.name == "submit":
                    submitted = use.input
                    results.append({"type": "tool_result", "tool_use_id": use.id, "content": "received"})
                    continue
                tool = by_name.get(use.name)
                if tool is None:
                    out, err = f"Error: unknown tool {use.name}", True
                else:
                    self._log(f"  tool {use.name}({json.dumps(use.input)[:120]})")
                    out, err = run_tool(tool, use.input)
                results.append({"type": "tool_result", "tool_use_id": use.id, "content": out, "is_error": err})
            if submitted is not None:
                return submitted
            messages.append({"role": "user", "content": results})
        raise LLMError(f"agent did not submit an answer within {max_turns} turns")


@dataclass
class ScriptedLLM:
    """Deterministic LLM for tests.

    ``json_handler(system, prompt, schema) -> dict``
    ``agent_handler(system, prompt, tools_by_name) -> dict`` - may call tools directly.
    """

    json_handler: Callable[[str, str, dict], dict] | None = None
    agent_handler: Callable[[str, str, dict[str, Tool]], dict] | None = None
    calls: list[dict] = field(default_factory=list)

    def json(self, system: str, prompt: str, schema: dict) -> dict:
        self.calls.append({"kind": "json", "system": system, "prompt": prompt})
        if self.json_handler is None:
            raise LLMError("ScriptedLLM has no json_handler")
        return self.json_handler(system, prompt, schema)

    def agent(self, system, prompt, tools, submit_schema, max_turns=30) -> dict:
        self.calls.append({"kind": "agent", "system": system, "prompt": prompt})
        if self.agent_handler is None:
            raise LLMError("ScriptedLLM has no agent_handler")
        return self.agent_handler(system, prompt, {t.name: t for t in tools})
