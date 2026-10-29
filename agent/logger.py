"""Structured run logger: every event is appended to a JSONL file (one JSON object per line)
and, optionally, pretty-printed to the console."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel


def _short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


class RunLogger:
    """One logger instance per agent run."""

    def __init__(self, log_dir: Path, label: str = "run", console: bool = True) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_id = uuid.uuid4().hex[:8]
        self.path = log_dir / f"{label}_{stamp}_{self.run_id}.jsonl"
        self.events: List[Dict[str, Any]] = []
        self._console: Optional[Console] = Console() if console else None

    # -- file logging ---------------------------------------------------------
    def log(self, event: str, **fields: Any) -> None:
        record: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "seq": len(self.events) + 1,
            "event": event,
            **fields,
        }
        self.events.append(record)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._print(record)

    # -- console output -------------------------------------------------------
    def _print(self, rec: Dict[str, Any]) -> None:
        if self._console is None:
            return
        out, event = self._console.print, rec["event"]
        step = f"[dim]step {rec['step']}[/dim] " if "step" in rec else ""

        if event == "run_start":
            out(Panel(escape(rec["task"]), title=f"Task  (run {self.run_id})", border_style="blue"))
        elif event == "thought":
            out(f"{step}[yellow]thought:[/yellow] {escape(_short(rec['text'], 500))}")
        elif event == "tool_call":
            out(f"{step}[cyan]call[/cyan] {rec['tool']}({escape(_short(rec['args'], 200))})")
        elif event == "validation_error":
            out(f"{step}[red]invalid arguments[/red] {escape(rec['error'])}")
        elif event == "tool_attempt" and not rec["ok"]:
            out(f"{step}[yellow]attempt {rec['attempt']} failed[/yellow] {escape(_short(rec['error'], 200))}")
        elif event == "tool_result":
            if rec["ok"]:
                out(f"{step}[green]ok[/green] {escape(_short(rec['result'], 300))}")
            else:
                out(f"{step}[red]failed[/red] {escape(_short(rec['error'], 300))}")
        elif event == "loop_guard":
            out(f"{step}[red]loop guard[/red] {escape(rec['message'])}")
        elif event == "final_answer":
            out(Panel(escape(rec["text"]), title="Answer", border_style="green"))
        elif event == "step_limit_reached":
            out(Panel(escape(rec["message"]), title="Step limit reached", border_style="red"))
        elif event == "error":
            out(f"[bold red]error:[/bold red] {escape(rec['message'])}")
        elif event == "run_end":
            out(
                f"[dim]status={rec['status']}  steps={rec['steps']}  tool_calls={rec['tool_calls']}  "
                f"time={rec['duration_s']}s  log={self.path.name}[/dim]"
            )
