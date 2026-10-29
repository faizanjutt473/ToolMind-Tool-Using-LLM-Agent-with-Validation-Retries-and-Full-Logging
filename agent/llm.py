"""LLM layer: provider-neutral message types and the Gemini client.

The agent only talks to the small interface below, so the model can be replaced
(or faked in unit tests) without touching the agent loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from agent import config


class LLMError(Exception):
    """The language model could not be reached or returned an unusable response."""


# --------------------------------------------------------------------------
# Neutral conversation types
# --------------------------------------------------------------------------
@dataclass
class ToolCall:
    name: str
    args: Dict[str, Any]


@dataclass
class ToolResult:
    name: str
    payload: Dict[str, Any]  # {"result": ...} or {"error": ...}
    ok: bool


@dataclass
class UserMessage:
    text: str


@dataclass
class ModelMessage:
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    raw: Any = None  # provider-specific content, replayed as-is (keeps Gemini thought signatures)


@dataclass
class ToolResultsMessage:
    results: List[ToolResult]


Message = Union[UserMessage, ModelMessage, ToolResultsMessage]


@dataclass
class LLMResponse:
    text: str = ""
    thought: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    raw: Any = None
    finish_reason: Optional[str] = None
    usage: Dict[str, Optional[int]] = field(default_factory=dict)
    latency_ms: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.text.strip() and not self.tool_calls


# --------------------------------------------------------------------------
# Gemini implementation
# --------------------------------------------------------------------------
_RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, genai_errors.APIError):
        return exc.code in _RETRYABLE_HTTP_CODES
    return isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError))


class GeminiClient:
    def __init__(
        self,
        api_key: str = config.GEMINI_API_KEY,
        model: str = config.GEMINI_MODEL,
        include_thoughts: bool = config.GEMINI_INCLUDE_THOUGHTS,
        temperature: Optional[float] = config.GEMINI_TEMPERATURE,
        max_retries: int = config.LLM_MAX_RETRIES,
    ) -> None:
        if not api_key:
            raise LLMError("GEMINI_API_KEY is missing. Add it to the .env file in the project root.")
        self.model = model
        self.include_thoughts = include_thoughts
        self.temperature = temperature
        self.max_retries = max_retries
        self._client = genai.Client(api_key=api_key)

    # -- public API -------------------------------------------------------------
    def generate(
        self,
        transcript: Sequence[Message],
        system_prompt: str,
        tool_declarations: List[Dict[str, Any]],
    ) -> LLMResponse:
        gen_config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[types.Tool(function_declarations=tool_declarations)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=self.temperature,
            thinking_config=types.ThinkingConfig(include_thoughts=True) if self.include_thoughts else None,
        )
        contents = self._to_contents(transcript)

        started = time.perf_counter()
        try:
            response = None
            for attempt in Retrying(
                reraise=True,
                stop=stop_after_attempt(self.max_retries),
                wait=wait_exponential(multiplier=2, min=2, max=30),
                retry=retry_if_exception(_is_retryable),
            ):
                with attempt:
                    response = self._client.models.generate_content(
                        model=self.model, contents=contents, config=gen_config
                    )
        except Exception as exc:
            raise LLMError(f"Gemini request failed ({self.model}): {exc}") from exc

        parsed = self._parse(response)
        parsed.latency_ms = int((time.perf_counter() - started) * 1000)
        return parsed

    def list_models(self) -> List[str]:
        """Model names that support generateContent (useful when a model name is rejected)."""
        names = []
        for model in self._client.models.list():
            if "generateContent" in (model.supported_actions or []):
                names.append((model.name or "").replace("models/", ""))
        return sorted(names)

    # -- conversion helpers -----------------------------------------------------
    @staticmethod
    def _to_contents(transcript: Sequence[Message]) -> List[Any]:
        contents: List[Any] = []
        for msg in transcript:
            if isinstance(msg, UserMessage):
                contents.append(types.Content(role="user", parts=[types.Part.from_text(text=msg.text)]))
            elif isinstance(msg, ModelMessage):
                if msg.raw is not None:
                    contents.append(msg.raw)
                else:
                    contents.append(
                        types.Content(role="model", parts=[types.Part.from_text(text=msg.text or "(no output)")])
                    )
            else:
                parts = [types.Part.from_function_response(name=r.name, response=r.payload) for r in msg.results]
                contents.append(types.Content(role="user", parts=parts))
        return contents

    @staticmethod
    def _parse(response: Any) -> LLMResponse:
        usage = getattr(response, "usage_metadata", None)
        usage_dict = {
            "prompt_tokens": getattr(usage, "prompt_token_count", None),
            "output_tokens": getattr(usage, "candidates_token_count", None),
            "total_tokens": getattr(usage, "total_token_count", None),
        }
        candidates = response.candidates or []
        if not candidates:
            feedback = getattr(response, "prompt_feedback", None)
            reason = getattr(feedback, "block_reason", None)
            return LLMResponse(finish_reason=f"no_candidates:{reason}", usage=usage_dict)

        candidate = candidates[0]
        finish = str(candidate.finish_reason) if candidate.finish_reason is not None else None
        content = candidate.content
        if content is None or not content.parts:
            return LLMResponse(finish_reason=finish, usage=usage_dict)

        thoughts: List[str] = []
        texts: List[str] = []
        calls: List[ToolCall] = []
        for part in content.parts:
            if part.function_call is not None:
                calls.append(ToolCall(name=part.function_call.name or "", args=dict(part.function_call.args or {})))
            elif part.text:
                (thoughts if part.thought else texts).append(part.text)

        return LLMResponse(
            text="\n".join(texts).strip(),
            thought="\n".join(thoughts).strip(),
            tool_calls=calls,
            raw=content,
            finish_reason=finish,
            usage=usage_dict,
        )
