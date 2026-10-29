"""Pydantic argument schemas for every tool.

The same models are used for two things:
  1. validating the arguments produced by the LLM before a tool runs, and
  2. generating the function declarations that are sent to Gemini.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agent import config

_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|attach|detach|pragma|vacuum|reindex)\b",
    re.IGNORECASE,
)


class _StrictArgs(BaseModel):
    """Base class: strip whitespace and reject unknown arguments."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WebSearchArgs(_StrictArgs):
    query: str = Field(
        ..., min_length=2, max_length=300,
        description="The search query, e.g. 'population of Faisalabad'.",
    )
    max_results: int = Field(
        5, ge=1, le=config.WEB_MAX_RESULTS,
        description="Number of results to return (1-10). Default 5.",
    )


class CalculatorArgs(_StrictArgs):
    expression: str = Field(
        ..., min_length=1, max_length=300,
        description=(
            "A single math expression, e.g. '(361000 + 347000) / 2' or 'sqrt(144) + 0.1 * 500'. "
            "Use * for multiplication, ** for powers and 0.15 * x for percentages "
            "(the % sign is the modulo operator)."
        ),
    )


class DBQueryArgs(_StrictArgs):
    sql: str = Field(
        ..., min_length=6, max_length=2000,
        description="One read-only SQLite SELECT statement. Only SELECT is allowed.",
    )

    @field_validator("sql")
    @classmethod
    def _must_be_read_only_select(cls, value: str) -> str:
        sql = value.strip().rstrip(";").strip()
        without_literals = _STRING_LITERAL.sub("''", sql)

        if ";" in without_literals:
            raise ValueError("only a single SQL statement is allowed")
        if "--" in without_literals or "/*" in without_literals:
            raise ValueError("SQL comments are not allowed")
        if not re.match(r"^(select|with)\b", without_literals, re.IGNORECASE):
            raise ValueError("only SELECT queries are allowed (read-only database)")
        match = _FORBIDDEN_SQL.search(without_literals)
        if match:
            raise ValueError(f"forbidden keyword '{match.group(0).upper()}': the database is read-only")
        return sql


class FileReaderArgs(_StrictArgs):
    path: str = Field(
        ..., min_length=1, max_length=260,
        description="File name inside the data folder, e.g. 'notes.txt' or 'sales.csv'. "
                    "Supported types: .txt .md .csv .json .pdf",
    )
    max_chars: int = Field(
        config.FILE_DEFAULT_MAX_CHARS, ge=100, le=config.FILE_HARD_MAX_CHARS,
        description="Maximum number of characters to return (100-20000). Default 4000.",
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
_TYPE_MAP = {"string": "STRING", "integer": "INTEGER", "number": "NUMBER", "boolean": "BOOLEAN"}


def gemini_parameters(model: type[BaseModel]) -> Dict[str, Any]:
    """Convert a Pydantic model into the parameter schema Gemini expects."""
    schema = model.model_json_schema()
    properties: Dict[str, Any] = {}
    for name, spec in schema.get("properties", {}).items():
        entry: Dict[str, Any] = {"type": _TYPE_MAP[spec["type"]]}
        if "description" in spec:
            entry["description"] = spec["description"]
        properties[name] = entry
    return {"type": "OBJECT", "properties": properties, "required": schema.get("required", [])}


def format_validation_error(exc: ValidationError) -> str:
    """Turn a Pydantic error into a short message the LLM can act on."""
    problems: List[str] = []
    for err in exc.errors():
        field = ".".join(str(part) for part in err["loc"]) or "arguments"
        message = err["msg"].replace("Value error, ", "")
        problems.append(f"{field}: {message}")
    return "; ".join(problems)
