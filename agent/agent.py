"""The agent loop: LLM -> tool selection -> argument validation -> execution with retries
-> result back to the LLM, until a final answer or the step limit."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import ValidationError

from agent import config
from agent import tools as toolbox
from agent.llm import (
    GeminiClient,
    LLMError,
    ModelMessage,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
    UserMessage,
)
from agent.logger import RunLogger
from agent.schemas import format_validation_error
from agent.tools import ToolError, TransientToolError

SYSTEM_PROMPT = """You are ToolMind, an AI agent that completes multi-step tasks by calling tools.

DATA YOU CAN ACCESS
Database tables (read-only SQLite):
{schema}
Files in the data folder: {files}

RULES
1. Before every tool call, write one short sentence explaining why you are making it.
2. Choose the right tool: company data -> db_query; named files or documents -> file_reader; external or
   current facts -> web_search; ALL arithmetic, percentages, averages and conversions -> calculator
   (never calculate in your head).
3. Break the task into steps and feed the result of one tool into the next. Independent calls may be made
   in the same turn.
4. If a tool returns an error, read the message, fix the arguments or change approach, and try again.
   Never repeat an identical failing call.
5. The database and files are read-only. Refuse requests to modify, delete or write data and explain why.
6. Only files inside the data folder may be read. Refuse anything else.
7. If no available tool can perform what the user asks (for example booking flights or sending email),
   say so clearly. Do not pretend, and do not invent data.
8. Only state numbers and facts that came from a tool result. If you could not obtain something, say so.
9. When finished, reply with a concise final answer that includes the key numbers and the sources used.
   Reply in the same language as the user's request."""


@dataclass
class ToolCallRecord:
    step: int
    name: str
    args: Dict[str, Any]
    ok: bool
    attempts: int
    duration_ms: int
    error: Optional[str] = None
    result: Any = None


@dataclass
class AgentResult:
    run_id: str
    task: str
    status: str  # completed | step_limit | error
    answer: str
    steps: int
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    duration_s: float = 0.0
    log_path: Optional[Path] = None
    error: Optional[str] = None

    @property
    def tools_used(self) -> List[str]:
        return [c.name for c in self.tool_calls]


class Agent:
    def __init__(
        self,
        llm: Optional[Any] = None,
        log_dir: Path = config.LOG_DIR,
        verbose: bool = True,
        max_steps: int = config.MAX_STEPS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.llm = llm if llm is not None else GeminiClient()
        self.log_dir = log_dir
        self.verbose = verbose
        self.max_steps = max_steps
        self._sleep = sleep

    # ------------------------------------------------------------------ public
    def run(self, task: str, max_steps: Optional[int] = None, label: str = "run") -> AgentResult:
        limit = max_steps if max_steps is not None else self.max_steps
        log = RunLogger(self.log_dir, label=label, console=self.verbose)
        started = time.perf_counter()

        system_prompt = SYSTEM_PROMPT.format(
            schema=toolbox.get_db_schema(),
            files=", ".join(toolbox.list_data_files()) or "none",
        )
        declarations = [tool.declaration() for tool in toolbox.TOOLS.values()]
        transcript: List[Any] = [UserMessage(task)]
        records: List[ToolCallRecord] = []
        call_counts: Dict[Tuple[str, str], int] = {}

        log.log(
            "run_start", task=task, model=getattr(self.llm, "model", "unknown"),
            max_steps=limit, tools=list(toolbox.TOOLS), tool_max_attempts=config.TOOL_MAX_ATTEMPTS,
        )

        status, answer, error, steps_used, empty_streak = "step_limit", "", None, 0, 0

        for step in range(1, limit + 1):
            steps_used = step
            try:
                response = self.llm.generate(transcript, system_prompt, declarations)
            except LLMError as exc:
                status, error = "error", str(exc)
                answer = f"The agent stopped because the language model failed: {exc}"
                log.log("error", step=step, message=str(exc))
                break

            log.log(
                "llm_response", step=step, latency_ms=response.latency_ms, finish_reason=response.finish_reason,
                usage=response.usage, tool_calls=[c.name for c in response.tool_calls],
            )
            if response.thought:
                log.log("thought", step=step, source="model_thinking_summary", text=response.thought)

            # --- model returned nothing usable -----------------------------------
            if response.is_empty:
                empty_streak += 1
                log.log("empty_response", step=step, finish_reason=response.finish_reason)
                if empty_streak >= 2:
                    status, error = "error", "The model returned two empty responses in a row."
                    answer = f"The agent stopped: {error}"
                    log.log("error", step=step, message=error)
                    break
                transcript.append(ModelMessage(text="(no output)"))
                transcript.append(UserMessage("Your last reply was empty. Call a tool or give your final answer."))
                continue
            empty_streak = 0

            transcript.append(ModelMessage(text=response.text, tool_calls=response.tool_calls, raw=response.raw))

            # --- final answer ----------------------------------------------------
            if not response.tool_calls:
                status, answer = "completed", response.text
                log.log("final_answer", step=step, text=answer)
                break

            # --- narration before tool calls counts as visible reasoning --------
            if response.text:
                log.log("thought", step=step, source="narration", text=response.text)

            results: List[ToolResult] = []
            for call in response.tool_calls:
                result, record = self._execute(step, call, call_counts, log)
                results.append(result)
                records.append(record)
            transcript.append(ToolResultsMessage(results))

        if status == "step_limit":
            answer = self._step_limit_message(limit, records)
            log.log("step_limit_reached", step=steps_used, message=answer)

        duration = round(time.perf_counter() - started, 2)
        log.log(
            "run_end", status=status, steps=steps_used, tool_calls=len(records),
            duration_s=duration, answer=answer,
        )
        return AgentResult(
            run_id=log.run_id, task=task, status=status, answer=answer, steps=steps_used,
            tool_calls=records, duration_s=duration, log_path=log.path, error=error,
        )

    # ----------------------------------------------------------------- internals
    def _execute(
        self, step: int, call: ToolCall, call_counts: Dict[Tuple[str, str], int], log: RunLogger
    ) -> Tuple[ToolResult, ToolCallRecord]:
        """Validate and run one tool call. Never raises: failures become error payloads for the LLM."""
        log.log("tool_call", step=step, tool=call.name, args=call.args)

        def failure(message: str, attempts: int = 0, duration_ms: int = 0) -> Tuple[ToolResult, ToolCallRecord]:
            log.log("tool_result", step=step, tool=call.name, ok=False, error=message, attempts=attempts)
            return (
                ToolResult(call.name, {"error": message}, ok=False),
                ToolCallRecord(step, call.name, call.args, False, attempts, duration_ms, error=message),
            )

        # 1. tool selection -------------------------------------------------------
        tool = toolbox.TOOLS.get(call.name)
        if tool is None:
            message = f"Unknown tool '{call.name}'. Available tools: {', '.join(toolbox.TOOLS)}."
            log.log("validation_error", step=step, tool=call.name, error=message)
            return failure(message)

        # 2. argument validation --------------------------------------------------
        try:
            args = tool.args_model.model_validate(call.args)
        except ValidationError as exc:
            message = f"Invalid arguments for {call.name}: {format_validation_error(exc)}"
            log.log("validation_error", step=step, tool=call.name, args=call.args, error=message)
            return failure(message)

        # 3. loop guard -----------------------------------------------------------
        key = (call.name, json.dumps(call.args, sort_keys=True, default=str))
        if call_counts.get(key, 0) >= config.MAX_IDENTICAL_CALLS:
            message = (
                f"This exact {call.name} call was already made {call_counts[key]} times. "
                "Reuse the earlier result, change the arguments, or give your final answer."
            )
            log.log("loop_guard", step=step, tool=call.name, message=message)
            return failure(message)
        call_counts[key] = call_counts.get(key, 0) + 1

        # 4. execution with retries ----------------------------------------------
        started = time.perf_counter()
        last_error = "unknown error"
        attempts = 0
        for attempt in range(1, config.TOOL_MAX_ATTEMPTS + 1):
            attempts = attempt
            t0 = time.perf_counter()
            try:
                output = tool.func(args)
            except TransientToolError as exc:
                last_error, retryable = str(exc), True
            except ToolError as exc:
                last_error, retryable = str(exc), False
            except Exception as exc:  # unexpected bug or environment problem: treat as retryable
                last_error, retryable = f"{type(exc).__name__}: {exc}", True
            else:
                payload = self._fit_payload(output)
                duration_ms = int((time.perf_counter() - started) * 1000)
                log.log(
                    "tool_attempt", step=step, tool=call.name, attempt=attempt, ok=True,
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                )
                log.log(
                    "tool_result", step=step, tool=call.name, ok=True, result=payload,
                    attempts=attempt, duration_ms=duration_ms,
                )
                return (
                    ToolResult(call.name, {"result": payload}, ok=True),
                    ToolCallRecord(step, call.name, call.args, True, attempt, duration_ms, result=payload),
                )

            log.log(
                "tool_attempt", step=step, tool=call.name, attempt=attempt, ok=False, error=last_error,
                retryable=retryable, duration_ms=int((time.perf_counter() - t0) * 1000),
            )
            if not retryable or attempt == config.TOOL_MAX_ATTEMPTS:
                break
            delay = config.TOOL_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            log.log("retry_wait", step=step, tool=call.name, next_attempt=attempt + 1, delay_s=delay)
            self._sleep(delay)

        return failure(last_error, attempts, int((time.perf_counter() - started) * 1000))

    @staticmethod
    def _fit_payload(output: Any) -> Any:
        """Make the tool output JSON-safe and cap its size before it goes back to the model."""
        text = json.dumps(output, ensure_ascii=False, default=str)
        if len(text) <= config.MAX_TOOL_OUTPUT_CHARS:
            return json.loads(text)
        return {
            "truncated_output": text[: config.MAX_TOOL_OUTPUT_CHARS],
            "note": "Output was too large and has been cut. Narrow the request.",
        }

    @staticmethod
    def _step_limit_message(limit: int, records: List[ToolCallRecord]) -> str:
        done = "; ".join(f"{r.name} ({'ok' if r.ok else 'failed'})" for r in records) or "no tools were called"
        return (
            f"I stopped because the step limit of {limit} was reached before the task was finished. "
            f"Work done so far: {done}. Please simplify the request or raise MAX_STEPS."
        )
