"""
lib_agents.py
─────────────────────────────────────────────────────────────────────────────
Abstract base class for Signal Mesh LLM agents.

All concrete agents (lib_agents_claude.py, lib_agents_gemini.py, …) must
subclass BaseAgent and implement:
  • fetch_data(prompt, timeout)  → dict  — send prompt, return parsed JSON
  • display_verbose(input, output)        — print prompt + response verbosely
"""

from abc import ABC, abstractmethod


class BaseAgent(ABC):
    """Abstract base for every LLM backend used by Signal Mesh."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    @property
    def name(self) -> str:
        """Short human-readable label, e.g. 'Claude' or 'Gemini'."""
        return self.__class__.__name__.replace("Agent", "")

    @abstractmethod
    def fetch_data(self, prompt: str, timeout: int = 120) -> dict:
        """
        Send *prompt* to the LLM and return a parsed JSON dict.

        On any failure the returned dict must contain at least:
            {"error": "<reason>", "signal": "HOLD", "factor_score": 50}
        so callers can handle it uniformly without isinstance checks.
        """

    @abstractmethod
    def display_verbose(self, prompt_input: str, prompt_output: str) -> None:
        """
        Print the raw prompt and raw LLM response for debugging.
        Implementations should prefix output with the agent name so
        multi-agent runs are easy to read.
        """
