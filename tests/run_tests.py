"""Run the multi-step test suite against the real LLM and write the results.

Usage (from the project root):
    python -m tests.run_tests                 run all tasks
    python -m tests.run_tests --only 1 5 9    run selected tasks
    python -m tests.run_tests --delay 8       wait 8 s between tasks (free-tier rate limits)

Outputs: results/test_results.json and results/test_results.md. Every task also writes its own
JSONL trace into logs/.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console  # noqa: E402

from agent import config  # noqa: E402
from agent.agent import Agent  # noqa: E402
from agent.llm import GeminiClient, LLMError  # noqa: E402
from tests.tasks import TASKS  # noqa: E402

console = Console()


def _cell(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split()).replace("|", "/")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the ToolMind test suite")
    parser.add_argument("--only", type=int, nargs="*", help="task ids to run")
    parser.add_argument("--delay", type=float, default=5.0, help="seconds to wait between tasks")
    parser.add_argument("--verbose", action="store_true", help="print the full trace for every task")
    args = parser.parse_args()

    try:
        llm = GeminiClient()
    except LLMError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        return 1

    agent = Agent(llm=llm, verbose=args.verbose)
    selected = [t for t in TASKS if not args.only or t.id in args.only]
    rows = []

    for index, task in enumerate(selected):
        console.print(f"[bold]Task {task.id:>2}[/bold] {task.name} ...", end=" ")
        result = agent.run(task.prompt, max_steps=task.max_steps, label=f"task{task.id:02d}")
        passed, reason = task.check(result)
        console.print("[green]PASS[/green]" if passed else "[red]FAIL[/red]", f"- {reason}")
        rows.append({
            "id": task.id, "name": task.name, "prompt": task.prompt, "required_tools": task.required_tools,
            "tools_used": result.tools_used, "status": result.status, "steps": result.steps,
            "duration_s": result.duration_s, "passed": passed, "reason": reason,
            "answer": result.answer, "log": result.log_path.name if result.log_path else None,
        })
        if index < len(selected) - 1:
            time.sleep(args.delay)

    config.RESULTS_DIR.mkdir(exist_ok=True)
    (config.RESULTS_DIR / "test_results.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    passed_count = sum(r["passed"] for r in rows)
    lines = [
        "# Test Results",
        "",
        f"Run on {datetime.now():%Y-%m-%d %H:%M} with model `{llm.model}` - **{passed_count}/{len(rows)} passed**",
        "",
        "| # | Task | Tools used | Steps | Time (s) | Result | Reason / failure |",
        "|---|------|------------|-------|----------|--------|------------------|",
    ]
    for r in rows:
        lines.append(
            f"| {r['id']} | {_cell(r['name'], 45)} | {_cell(', '.join(r['tools_used']) or '-', 50)} | {r['steps']} | "
            f"{r['duration_s']} | {'PASS' if r['passed'] else 'FAIL'} | {_cell(r['reason'])} |"
        )
    lines += ["", "## Final answers", ""]
    for r in rows:
        lines += [f"**Task {r['id']} - {r['name']}** (`{r['log']}`)", "", f"> {_cell(r['answer'], 600)}", ""]
    (config.RESULTS_DIR / "test_results.md").write_text("\n".join(lines), encoding="utf-8")

    console.print(f"\n[bold]{passed_count}/{len(rows)} passed.[/bold] Saved results/test_results.md and .json")
    return 0 if passed_count == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
