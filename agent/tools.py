"""The four tools the agent can call: web_search, calculator, db_query, file_reader.

Error model
-----------
* ToolError           - deterministic failure; retrying with the same arguments is pointless.
                        The message is sent back to the LLM so it can correct itself.
* TransientToolError  - temporary failure (network, timeout, locked DB); the agent retries it.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List

from ddgs import DDGS
from pydantic import BaseModel
from pypdf import PdfReader
from simpleeval import SimpleEval

from agent import config
from agent.schemas import (
    CalculatorArgs,
    DBQueryArgs,
    FileReaderArgs,
    WebSearchArgs,
    gemini_parameters,
)


class ToolError(Exception):
    """A tool failed in a way that retrying will not fix."""


class TransientToolError(ToolError):
    """A tool failed temporarily; the caller may retry."""


# --------------------------------------------------------------------------
# 1. web_search
# --------------------------------------------------------------------------
def web_search(args: WebSearchArgs) -> Dict[str, Any]:
    try:
        raw = DDGS(timeout=config.WEB_TIMEOUT_SECONDS).text(args.query, max_results=args.max_results)
    except Exception as exc:  # ddgs raises several exception types; all are network related
        if "no results" in str(exc).lower():
            raw = []
        else:
            raise TransientToolError(f"Web search failed: {exc}") from exc

    results = [
        {
            "title": item.get("title", ""),
            "url": item.get("href", ""),
            "snippet": (item.get("body") or "")[:350],
        }
        for item in raw or []
    ]
    return {"query": args.query, "result_count": len(results), "results": results}


# --------------------------------------------------------------------------
# 2. calculator
# --------------------------------------------------------------------------
_MATH_FUNCTIONS: Dict[str, Callable[..., Any]] = {
    "sqrt": math.sqrt, "abs": abs, "round": round, "min": min, "max": max,
    "floor": math.floor, "ceil": math.ceil, "log": math.log, "log10": math.log10,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
}
_MATH_NAMES: Dict[str, float] = {"pi": math.pi, "e": math.e}


def calculator(args: CalculatorArgs) -> Dict[str, Any]:
    # '^' is bitwise XOR in Python; models almost always mean "power".
    expression = args.expression.replace("^", "**").replace("×", "*").replace("÷", "/")
    try:
        value = SimpleEval(functions=_MATH_FUNCTIONS, names=_MATH_NAMES).eval(expression)
    except ZeroDivisionError as exc:
        raise ToolError("Division by zero.") from exc
    except Exception as exc:  # simpleeval raises many specific errors
        raise ToolError(f"Invalid expression: {exc}") from exc

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ToolError("The result is not a finite number.")
        value = int(value) if value.is_integer() and abs(value) < 1e15 else round(value, 10)
    return {"expression": args.expression, "result": value}


# --------------------------------------------------------------------------
# 3. db_query  (read-only SQLite)
# --------------------------------------------------------------------------
# sqlite3 action codes: SELECT=21, READ=20, FUNCTION=31, RECURSIVE=33
_ALLOWED_SQLITE_ACTIONS = {20, 21, 31, 33}


def _authorizer(action: int, *_: Any) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED_SQLITE_ACTIONS else sqlite3.SQLITE_DENY


def get_db_schema() -> str:
    """Human-readable description of the tables, used in the system prompt and in errors."""
    if not config.DB_PATH.exists():
        return "(database not found - run setup_data.py)"
    conn = sqlite3.connect(config.DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        lines = []
        for (table,) in tables:
            columns = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            cols = ", ".join(f"{col[1]} {col[2]}" for col in columns)
            lines.append(f"{table}({cols})")
        return "\n".join(lines) or "(no tables)"
    finally:
        conn.close()


def db_query(args: DBQueryArgs) -> Dict[str, Any]:
    if not config.DB_PATH.exists():
        raise ToolError("Database file not found. Run setup_data.py first.")

    # Defence in depth: validated SELECT + read-only connection + authorizer.
    conn = sqlite3.connect(config.DB_PATH.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.set_authorizer(_authorizer)
    try:
        cursor = conn.execute(args.sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(config.DB_MAX_ROWS + 1)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise TransientToolError(f"Database is busy: {exc}") from exc
        raise ToolError(f"SQL error: {exc}. Available tables:\n{get_db_schema()}") from exc
    except sqlite3.Error as exc:
        raise ToolError(f"SQL error: {exc}. Available tables:\n{get_db_schema()}") from exc
    finally:
        conn.close()

    truncated = len(rows) > config.DB_MAX_ROWS
    rows = rows[: config.DB_MAX_ROWS]
    return {
        "columns": columns,
        "rows": [list(row) for row in rows],
        "row_count": len(rows),
        "truncated": truncated,
    }


# --------------------------------------------------------------------------
# 4. file_reader  (sandboxed to the data/ folder)
# --------------------------------------------------------------------------
def list_data_files() -> List[str]:
    if not config.DATA_DIR.exists():
        return []
    return sorted(
        p.name for p in config.DATA_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in config.ALLOWED_FILE_EXTENSIONS
    )


def _resolve_in_data_dir(raw_path: str) -> Path:
    cleaned = raw_path.strip().replace("\\", "/")
    if cleaned.lower().startswith("data/"):
        cleaned = cleaned[5:]
    root = config.DATA_DIR.resolve()
    candidate = (root / cleaned).resolve()
    if candidate != root and root not in candidate.parents:
        raise ToolError("Access denied: only files inside the data folder can be read.")
    return candidate


def file_reader(args: FileReaderArgs) -> Dict[str, Any]:
    path = _resolve_in_data_dir(args.path)

    if path.suffix.lower() not in config.ALLOWED_FILE_EXTENSIONS:
        raise ToolError(f"Unsupported file type '{path.suffix}'. Supported: {', '.join(config.ALLOWED_FILE_EXTENSIONS)}")
    if not path.is_file():
        raise ToolError(f"File not found: '{args.path}'. Available files: {', '.join(list_data_files()) or 'none'}")

    extra: Dict[str, Any] = {}
    try:
        if path.suffix.lower() == ".pdf":
            reader = PdfReader(str(path))
            if reader.is_encrypted:
                raise ToolError("The PDF is password protected.")
            text = "\n".join(
                f"--- page {i} ---\n{page.extract_text() or ''}" for i, page in enumerate(reader.pages, 1)
            )
            extra["pages"] = len(reader.pages)
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
            if path.suffix.lower() == ".csv":
                extra["data_rows"] = max(len([ln for ln in text.splitlines() if ln.strip()]) - 1, 0)
    except ToolError:
        raise
    except OSError as exc:
        raise ToolError(f"Could not read file: {exc}") from exc
    except Exception as exc:  # corrupt PDF etc.
        raise ToolError(f"Could not parse file: {exc}") from exc

    return {
        "path": path.name,
        "total_chars": len(text),
        "truncated": len(text) > args.max_chars,
        "content": text[: args.max_chars],
        **extra,
    }


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    func: Callable[[Any], Any]

    def declaration(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": gemini_parameters(self.args_model),
        }


TOOLS: Dict[str, Tool] = {
    tool.name: tool
    for tool in (
        Tool(
            "web_search",
            "Search the web for current or external facts (prices, populations, news, definitions). "
            "Returns titles, URLs and snippets. Do not use it for data stored in the company database or files.",
            WebSearchArgs, web_search,
        ),
        Tool(
            "calculator",
            "Evaluate a math expression exactly. Use it for ALL arithmetic, percentages, averages and unit "
            "conversions. Supports + - * / ** //, parentheses, sqrt, abs, round, min, max, floor, ceil, log, pi.",
            CalculatorArgs, calculator,
        ),
        Tool(
            "db_query",
            "Run a read-only SQL SELECT on the company SQLite database (employees and sales tables). "
            "Returns at most 50 rows. Prefer SQL aggregates (COUNT, SUM, AVG, GROUP BY) over fetching many rows.",
            DBQueryArgs, db_query,
        ),
        Tool(
            "file_reader",
            "Read a text, markdown, CSV, JSON or PDF file from the data folder. Use it when the user mentions a "
            "file or document. Returns the content (truncated if long).",
            FileReaderArgs, file_reader,
        ),
    )
}
