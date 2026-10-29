"""Command-line entry point.

Usage:
    python main.py "your task here"          run one task
    python main.py                            interactive mode
    python main.py --list-models              show model names that work with your API key
"""

from __future__ import annotations

import argparse
import sys

from rich.console import Console

from agent import config
from agent.agent import Agent
from agent.llm import GeminiClient, LLMError

console = Console()


def main() -> int:
    parser = argparse.ArgumentParser(description="ToolMind - tool-using AI agent")
    parser.add_argument("task", nargs="*", help="task for the agent (omit for interactive mode)")
    parser.add_argument("--max-steps", type=int, default=config.MAX_STEPS, help="LLM step limit per task")
    parser.add_argument("--quiet", action="store_true", help="do not print the step-by-step trace")
    parser.add_argument("--list-models", action="store_true", help="list available Gemini models and exit")
    args = parser.parse_args()

    try:
        llm = GeminiClient()
        if args.list_models:
            console.print("\n".join(llm.list_models()))
            return 0
    except LLMError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        return 1

    agent = Agent(llm=llm, verbose=not args.quiet, max_steps=args.max_steps)

    if args.task:
        result = agent.run(" ".join(args.task))
        if args.quiet:
            console.print(result.answer)
        return 0 if result.status == "completed" else 2

    console.print("[bold]ToolMind[/bold] - type a task, or 'exit' to quit.")
    while True:
        try:
            task = console.input("\n[bold blue]Task>[/bold blue] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return 0
        if task.lower() in {"exit", "quit", "q"}:
            return 0
        if task:
            result = agent.run(task)
            if args.quiet:
                console.print(result.answer)


if __name__ == "__main__":
    sys.exit(main())
