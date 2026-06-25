"""
lib_agents_gemini.py
─────────────────────────────────────────────────────────────────────────────
Concrete agent that sends prompts to Google Gemini via the google-genai SDK.

Setup:
    1. pip install google-genai
    2. Edit the project-root .env file and set:
           GEMINI_API_KEY=your_actual_key_here
       (Get a free key at https://aistudio.google.com/apikey)

Usage:
    from lib_agents_gemini import GeminiAgent
    agent = GeminiAgent(verbose=True)
    result = agent.fetch_data("Analyse AAPL and return JSON …")
"""

import json
import os
import re
import time
from pathlib import Path

from lib_agents import BaseAgent

RETRY_DELAY_LIMIT = 300  # seconds — above this we give up instead of waiting


def _parse_retry_delay(exc: Exception) -> float | None:
    """Extract retry delay in seconds from a 429 RESOURCE_EXHAUSTED exception."""
    msg = str(exc)
    # Prefer the structured retryDelay field: 'retryDelay': '29.08s'
    match = re.search(r"'retryDelay':\s*'([0-9.]+)s'", msg)
    if match:
        return float(match.group(1))
    # Fall back to the human-readable sentence: "Please retry in 29.08s"
    match = re.search(r"retry in ([0-9.]+)s", msg, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None

GEMINI_MODEL = "gemini-3-flash"   # change to any model you have access to


def _load_dotenv() -> None:
    """Walk up from this file to find a .env and load it into os.environ."""
    current = Path(__file__).resolve().parent
    for _ in range(6):
        env_file = current / ".env"
        if env_file.exists():
            with open(env_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, val = line.partition("=")
                        val = val.strip().strip('"').strip("'")
                        os.environ.setdefault(key.strip(), val)
            return
        parent = current.parent
        if parent == current:
            break
        current = parent


_load_dotenv()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

try:
    from google import genai as _genai   # pip install google-genai
    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False


class GeminiAgent(BaseAgent):
    """Agent that calls Gemini via the Google GenAI REST API."""

    def __init__(self, verbose: bool = False):
        super().__init__(verbose)
        self._prompt_count = 0
        if not _GENAI_AVAILABLE:
            raise ImportError(
                "google-genai is not installed. Run: pip install google-genai"
            )
        key = os.environ.get("GEMINI_API_KEY", "")
        if not key or key == "YOUR_GEMINI_API_KEY_HERE":
            raise ValueError(
                "GEMINI_API_KEY is not set. Add it to the project-root .env file:\n"
                "  GEMINI_API_KEY=your_actual_key_here"
            )
        self._client = _genai.Client(api_key=key)

    def fetch_data(self, prompt: str, timeout: int = 120) -> dict:
        return self._call_with_retry(prompt)

    def _call_with_retry(self, prompt: str, _is_retry: bool = False) -> dict:
        if not _is_retry:
            self._prompt_count += 1
        if self.verbose and not _is_retry:
            print(f"\n[GEMINI | Prompt #{self._prompt_count}] {'─' * 44}")
            print(f"[INPUT]\n{prompt}")
            print(f"{'─' * 60}")

        try:
            response = self._client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
            output = (response.text or "").strip()

            if self.verbose:
                print(f"[OUTPUT]\n{output}")
                print(f"{'─' * 60}\n")

            if not output:
                return {"error": "empty response from Gemini", "signal": "HOLD", "factor_score": 50}

            try:
                return json.loads(output)
            except json.JSONDecodeError:
                pass

            match = re.search(r"\{.*\}", output, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass

            return {"error": "unparseable response", "raw": output[:300], "signal": "HOLD", "factor_score": 50}

        except Exception as e:
            if ("429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)) and not _is_retry:
                return self._handle_rate_limit(e, prompt)
            return {"error": f"Gemini API error: {e}", "signal": "HOLD", "factor_score": 50}

    def _handle_rate_limit(self, exc: Exception, prompt: str) -> dict:
        delay = _parse_retry_delay(exc)
        print(f"\n[GEMINI] Rate limited (429 RESOURCE_EXHAUSTED).")
        if delay is None:
            print("[GEMINI] Could not parse retryDelay from error — giving up.")
            return {"error": f"Gemini API error: {exc}", "signal": "HOLD", "factor_score": 50}

        if delay > RETRY_DELAY_LIMIT:
            print(f"[GEMINI] retryDelay={delay:.1f}s exceeds {RETRY_DELAY_LIMIT}s limit — TIMEOUT.")
            return {"error": "timeout: retryDelay too long", "signal": "HOLD", "factor_score": 50}

        print(f"[GEMINI] retryDelay={delay:.1f}s — waiting before retry…")
        for remaining in range(int(delay), 0, -1):
            print(f"\r[GEMINI] Retrying in {remaining}s…  ", end="", flush=True)
            time.sleep(1)
        print("\r[GEMINI] Retrying now…              ")
        return self._call_with_retry(prompt, _is_retry=True)

    def display_verbose(self, prompt_input: str, prompt_output: str) -> None:
        print(f"\n[GEMINI] {'─' * 53}")
        print(f"[INPUT]\n{prompt_input}")
        print(f"{'─' * 60}")
        print(f"[OUTPUT]\n{prompt_output}")
        print(f"{'─' * 60}\n")
