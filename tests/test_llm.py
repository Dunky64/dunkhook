"""ClaudeLLM's loop logic against a fake Anthropic client (no network)."""

from types import SimpleNamespace as NS

import pytest

from dunkhook.core.llm import ClaudeLLM, LLMError, LLMRefusal, Tool
from dunkhook.core.schema import obj, string


def msg(stop, *blocks):
    return NS(stop_reason=stop, content=list(blocks), usage=NS(input_tokens=10, output_tokens=5), stop_details=None)


def text(t):
    return NS(type="text", text=t)


def use(id_, name, inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def create(self, **kw):
        # snapshot: the loop mutates the messages list after the call
        self.requests.append({**kw, "messages": list(kw["messages"])})
        return self.replies.pop(0)

    def stream(self, **kw):
        self.requests.append(kw)
        reply = self.replies.pop(0)

        class _S:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def get_final_message(s):
                return reply

        return _S()


def client(replies):
    m = FakeMessages(replies)
    return NS(beta=NS(messages=m)), m


def test_json_parses_structured_output_and_sends_fallbacks():
    c, m = client([msg("end_turn", text('{"a": 1}'))])
    llm = ClaudeLLM(client=c)
    assert llm.json("sys", "hi", obj(a=string())) == {"a": 1}
    req = m.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["fallbacks"] == "default"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in req and "temperature" not in req
    assert llm.usage == {"input_tokens": 10, "output_tokens": 5}


def test_json_refusal_and_truncation_raise():
    c, _ = client([msg("refusal", text("")), msg("max_tokens", text('{"a"'))])
    llm = ClaudeLLM(client=c)
    with pytest.raises(LLMRefusal):
        llm.json("s", "p", obj(a=string()))
    with pytest.raises(LLMError):
        llm.json("s", "p", obj(a=string()))


def test_agent_runs_tools_reports_errors_and_returns_submit():
    calls = []

    def echo(a):
        calls.append(a)
        return f"echo {a['x']}"

    def boom(a):
        raise ValueError("nope")

    tools = [
        Tool("echo", "", obj(x=string()), echo),
        Tool("boom", "", obj(x=string()), boom),
    ]
    c, m = client([
        msg("tool_use", text("looking"), use("t1", "echo", {"x": "1"}), use("t2", "boom", {"x": "2"})),
        msg("end_turn", text("thinking out loud, no tool")),
        msg("tool_use", use("t3", "submit", {"answer": "42"})),
    ])
    result = ClaudeLLM(client=c).agent("sys", "go", tools, obj(answer=string()))
    assert result == {"answer": "42"}
    assert calls == [{"x": "1"}]
    # Both tool results went back in ONE user message, error flagged.
    second = m.requests[1]["messages"]
    results = second[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert results[0]["content"] == "echo 1" and not results[0]["is_error"]
    assert results[1]["is_error"] and "nope" in results[1]["content"]
    # The assistant turn is appended unchanged (keeps thinking blocks intact).
    assert second[-2]["role"] == "assistant"
    # The submit tool is declared strict alongside user tools.
    names = [t["name"] for t in m.requests[0]["tools"]]
    assert names == ["echo", "boom", "submit"]
    assert all(t["strict"] for t in m.requests[0]["tools"])
    # A turn without tool calls gets a nudge.
    assert "submit" in m.requests[2]["messages"][-1]["content"]


def test_agent_gives_up_after_max_turns():
    c, _ = client([msg("end_turn", text("hmm"))] * 3)
    with pytest.raises(LLMError):
        ClaudeLLM(client=c).agent("s", "p", [], obj(a=string()), max_turns=3)
