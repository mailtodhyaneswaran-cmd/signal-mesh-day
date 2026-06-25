"""
lib_agents_claude.py
─────────────────────────────────────────────────────────────────────────────
Concrete agent that sends prompts to Claude via the Claude Code CLI
(subprocess, stdin piping).  No direct API key required — uses whatever
Claude account the CLI is authenticated with.

Usage:
    from lib_agents_claude import ClaudeAgent
    agent = ClaudeAgent(verbose=True)
    result = agent.fetch_data("Analyse AAPL and return JSON …")
"""

import json
import os
import re
import shutil
import subprocess
import sys

from lib_agents import BaseAgent

_CLAUDE_FALLBACK_PATHS = [
    os.path.expanduser("~\\.local\\bin\\claude.exe"),
    os.path.expanduser("~\\.local\\bin\\claude"),
]


def _find_claude() -> str:
    found = shutil.which("claude")
    if found:
        return found
    for path in _CLAUDE_FALLBACK_PATHS:
        if os.path.isfile(path):
            return path
    print("[ERROR] `claude` CLI not found. Install Claude Code and ensure it is on your PATH.")
    sys.exit(1)


class ClaudeAgent(BaseAgent):
    """Agent that calls Claude via the Claude Code CLI (subprocess / stdin)."""

    def __init__(self, verbose: bool = False):
        super().__init__(verbose)
        self._prompt_count = 0
        self._claude_bin = _find_claude()

    def fetch_data(self, prompt: str, timeout: int = 120) -> dict:
        self._prompt_count += 1
        if self.verbose:
            print(f"\n[CLAUDE | Prompt #{self._prompt_count}] {'─' * 44}")
            print(f"[INPUT]\n{prompt}")
            print(f"{'─' * 60}")
        try:
            result = subprocess.run(
                [self._claude_bin, "--print", "--dangerously-skip-permissions"],
                input=prompt,
                capture_output=True,
                encoding="utf-8",
                timeout=timeout,
            )
            output = result.stdout.strip()

            if self.verbose:
                print(f"[OUTPUT]\n{output}")
                print(f"{'─' * 60}\n")

            if not output:
                err = result.stderr.strip()
                return {"error": err or "empty response", "signal": "HOLD", "factor_score": 50}

            try:
                return json.loads(output)
            except json.JSONDecodeError:
                pass

            # Strip markdown fences then retry
            clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", output, flags=re.DOTALL).strip()
            try:
                return json.loads(clean)
            except json.JSONDecodeError:
                pass

            # Try first JSON object {...}
            match = re.search(r"\{.*\}", clean, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass

            # Try first JSON array [...] (bulk responses)
            match = re.search(r"\[.*\]", clean, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass

            return {"error": "unparseable response", "raw": output[:300], "signal": "HOLD", "factor_score": 50}

        except subprocess.TimeoutExpired:
            return {"error": "claude CLI timed out", "signal": "HOLD", "factor_score": 50}
        except FileNotFoundError:
            print("[ERROR] `claude` CLI not found. Install Claude Code and ensure it is on your PATH.")
            sys.exit(1)

    def display_verbose(self, prompt_input: str, prompt_output: str) -> None:
        print(f"\n[CLAUDE | Prompt #{self._prompt_count}] {'─' * 44}")
        print(f"[INPUT]\n{prompt_input}")
        print(f"{'─' * 60}")
        print(f"[OUTPUT]\n{prompt_output}")
        print(f"{'─' * 60}\n")
