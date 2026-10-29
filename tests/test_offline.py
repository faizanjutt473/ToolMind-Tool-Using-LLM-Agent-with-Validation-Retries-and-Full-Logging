"""Offline unit tests: no API key and no internet needed.

Run with:  pytest -q
A scripted fake LLM drives the real agent loop, so validation, retries, the step limit and
the loop guard are tested exactly as they run in production.
"""

from __future__ import annotations

import pytest

from agent import config
from agent import tools as toolbox
from agent.agent import Agent
from agent.llm import LLMResponse, ToolCall, ToolResultsMessage
from agent.schemas import CalculatorArgs, DBQueryArgs, FileReaderArgs
from agent.tools import TOOLS, Tool, ToolError, TransientToolError

needs_data = pytest.mark.skipif(not config.DB_PATH.exists(), reason="run setup_data.py first")


class ScriptedLLM:
    """Returns pre-defined responses in order and records every transcript it receives."""

    model = "scripted"

    def __init__(self, *responses: LLMResponse) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.transcripts = []

    def generate(self, transcript, system_prompt, tool_declarations):
        self.transcripts.append(list(transcript))
        self.calls += 1
        index = min(self.calls - 1, len(self._responses) - 1)  # repeat the last response if exhausted
        return self._responses[index]


def call(name, **args):
    return LLMResponse(text="Because I need it.", tool_calls=[ToolCall(name, args)])


def final(text):
    return LLMResponse(text=text)


def make_agent(llm, tmp_path, max_steps=8):
    return Agent(llm=llm, log_dir=tmp_path, verbose=False, max_steps=max_steps, sleep=lambda _s: None)


# ------------------------------------------------------------------ tools & validation
def test_calculator_basic_and_caret():
    assert toolbox.calculator(CalculatorArgs(expression="(2 + 3) * 4"))["result"] == 20
    assert toolbox.calculator(CalculatorArgs(expression="2^10"))["result"] == 1024
    assert toolbox.calculator(CalculatorArgs(expression="sqrt(144) + 0.1 * 500"))["result"] == 62


@pytest.mark.parametrize("expr", ["1/0", "__import__('os')", "10%", "abc + 1", "2**99999999"])
def test_calculator_rejects_bad_expressions(expr):
    with pytest.raises(ToolError):
        toolbox.calculator(CalculatorArgs(expression=expr))


@pytest.mark.parametrize("sql", [
    "DROP TABLE employees", "DELETE FROM employees", "UPDATE employees SET salary = 1",
    "SELECT 1; DROP TABLE employees", "SELECT * FROM employees -- comment",
    "WITH x AS (SELECT 1) DELETE FROM employees", "PRAGMA table_info(employees)",
])
def test_sql_validator_blocks_unsafe_statements(sql):
    with pytest.raises(ValueError):
        DBQueryArgs(sql=sql)


def test_sql_validator_allows_select_and_literals():
    assert DBQueryArgs(sql="SELECT * FROM employees WHERE name = 'update';").sql.endswith("'update'")


@needs_data
def test_db_query_returns_rows_and_is_read_only():
    out = toolbox.db_query(DBQueryArgs(sql="SELECT COUNT(*) AS n FROM employees"))
    assert out["columns"] == ["n"] and out["rows"][0][0] > 0
    with pytest.raises(ToolError):
        toolbox.db_query(DBQueryArgs(sql="SELECT * FROM table_that_does_not_exist"))


@needs_data
@pytest.mark.parametrize("path", ["../.env", "..\\setup_data.py", "/etc/passwd", "../../secret.txt"])
def test_file_reader_blocks_paths_outside_data(path):
    with pytest.raises(ToolError):
        toolbox.file_reader(FileReaderArgs(path=path))


@needs_data
def test_file_reader_reads_and_reports_missing():
    assert "Faisalabad" in toolbox.file_reader(FileReaderArgs(path="notes.txt"))["content"]
    assert "Faisalabad" in toolbox.file_reader(FileReaderArgs(path="data/notes.txt"))["content"]
    with pytest.raises(ToolError, match="not found"):
        toolbox.file_reader(FileReaderArgs(path="nope.txt"))


# ------------------------------------------------------------------ agent behaviour
def test_happy_path_multi_step(tmp_path):
    llm = ScriptedLLM(call("calculator", expression="6 * 7"), final("The answer is 42."))
    result = make_agent(llm, tmp_path).run("What is 6 times 7?")
    assert result.status == "completed" and result.answer == "The answer is 42."
    assert result.steps == 2 and result.tools_used == ["calculator"]
    assert result.tool_calls[0].ok and result.tool_calls[0].result["result"] == 42


def test_invalid_arguments_are_returned_to_llm_which_self_corrects(tmp_path):
    llm = ScriptedLLM(
        call("calculator"),                      # missing required argument
        call("calculator", expression="2 + 2"),  # corrected
        final("4"),
    )
    result = make_agent(llm, tmp_path).run("2+2?")
    assert result.status == "completed"
    assert [c.ok for c in result.tool_calls] == [False, True]
    fed_back = [m for m in llm.transcripts[1] if isinstance(m, ToolResultsMessage)][0]
    assert "Invalid arguments" in fed_back.results[0].payload["error"]


def test_unknown_tool_is_reported_not_crashed(tmp_path):
    llm = ScriptedLLM(call("send_email", to="x"), final("I cannot send email."))
    result = make_agent(llm, tmp_path).run("email bob")
    assert result.status == "completed" and not result.tool_calls[0].ok
    assert "Unknown tool" in result.tool_calls[0].error


def test_transient_failure_is_retried_then_succeeds(tmp_path, monkeypatch):
    attempts = {"n": 0}

    def flaky(args):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise TransientToolError("network down")
        return {"result": 3}

    monkeypatch.setitem(TOOLS, "calculator", Tool("calculator", "x", CalculatorArgs, flaky))
    llm = ScriptedLLM(call("calculator", expression="1+2"), final("3"))
    result = make_agent(llm, tmp_path).run("1+2")
    assert result.status == "completed" and result.tool_calls[0].ok
    assert result.tool_calls[0].attempts == 3 and attempts["n"] == 3


def test_retries_are_exhausted_and_error_reaches_llm(tmp_path, monkeypatch):
    def always_down(args):
        raise TransientToolError("network down")

    monkeypatch.setitem(TOOLS, "calculator", Tool("calculator", "x", CalculatorArgs, always_down))
    llm = ScriptedLLM(call("calculator", expression="1+2"), final("The tool is unavailable."))
    result = make_agent(llm, tmp_path).run("1+2")
    record = result.tool_calls[0]
    assert not record.ok and record.attempts == config.TOOL_MAX_ATTEMPTS and "network down" in record.error


def test_deterministic_errors_are_not_retried(tmp_path, monkeypatch):
    calls = {"n": 0}

    def bad(args):
        calls["n"] += 1
        raise ToolError("bad input")

    monkeypatch.setitem(TOOLS, "calculator", Tool("calculator", "x", CalculatorArgs, bad))
    llm = ScriptedLLM(call("calculator", expression="1+2"), final("failed"))
    make_agent(llm, tmp_path).run("x")
    assert calls["n"] == 1


def test_step_limit_stops_gracefully(tmp_path):
    llm = ScriptedLLM(*[call("calculator", expression=f"{i} + 1") for i in range(1, 20)])
    result = make_agent(llm, tmp_path, max_steps=3).run("never ends")
    assert result.status == "step_limit" and result.steps == 3 and llm.calls == 3
    assert "step limit" in result.answer.lower()


def test_loop_guard_blocks_repeated_identical_calls(tmp_path):
    llm = ScriptedLLM(*[call("calculator", expression="1 + 1")] * 4, final("done"))
    result = make_agent(llm, tmp_path).run("loop")
    assert [c.ok for c in result.tool_calls] == [True, True, False, False]
    assert "already made" in result.tool_calls[2].error


def test_llm_failure_ends_run_with_error_status(tmp_path):
    from agent.llm import LLMError

    class Broken:
        model = "broken"

        def generate(self, *a, **k):
            raise LLMError("quota exceeded")

    result = make_agent(Broken(), tmp_path).run("hi")
    assert result.status == "error" and "quota exceeded" in result.answer


def test_two_empty_responses_end_run(tmp_path):
    llm = ScriptedLLM(LLMResponse())
    result = make_agent(llm, tmp_path).run("hi")
    assert result.status == "error" and llm.calls == 2


def test_log_file_contains_every_event_type(tmp_path):
    llm = ScriptedLLM(call("calculator", expression="2 * 3"), final("6"))
    result = make_agent(llm, tmp_path).run("2*3")
    import json

    events = [json.loads(line)["event"] for line in result.log_path.read_text(encoding="utf-8").splitlines()]
    for expected in ("run_start", "llm_response", "thought", "tool_call", "tool_attempt", "tool_result",
                     "final_answer", "run_end"):
        assert expected in events


@needs_data
def test_destructive_sql_never_changes_database(tmp_path):
    before = toolbox.db_query(DBQueryArgs(sql="SELECT COUNT(*) FROM employees"))["rows"][0][0]
    llm = ScriptedLLM(call("db_query", sql="DELETE FROM employees"), final("I cannot modify the database."))
    result = make_agent(llm, tmp_path).run("delete everyone")
    after = toolbox.db_query(DBQueryArgs(sql="SELECT COUNT(*) FROM employees"))["rows"][0][0]
    assert before == after and not result.tool_calls[0].ok
