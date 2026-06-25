"""
lib_agents_mistral.py
─────────────────────────────────────────────────────────────────────────────
Concrete agent that sends prompts to Mistral AI via the mistralai SDK.

Setup:
    1. pip install mistralai
    2. Edit the project-root .env file and set:
           MISTRAL_API_KEY=your_actual_key_here
       (Get a key at https://console.mistral.ai/api-keys)

Usage:
    from lib_agents_mistral import MistralAgent
    agent = MistralAgent(verbose=True)
    result = agent.fetch_data("Analyse AAPL and return JSON …")
"""

import json
import os
import re
import time
from pathlib import Path

from lib_agents import BaseAgent

MISTRAL_MODEL     = "mistral-small-latest"   # swap to mistral-medium-latest / mistral-large-latest as needed
RETRY_DELAY_LIMIT = 300                       # seconds — give up if delay exceeds this


def _parse_retry_delay(exc: Exception) -> float | None:
    """Extract retry delay in seconds from a 429 Too Many Requests exception."""
    msg = str(exc)
    # "Retry-After: 30" or "retry_after=30.5"
    match = re.search(r"retry.?after[=:\s]+([0-9.]+)", msg, re.IGNORECASE)
    if match:
        return float(match.group(1))
    # "Please retry in 30s" or "wait 30 seconds"
    match = re.search(r"(?:retry in|wait)\s+([0-9.]+)\s*s", msg, re.IGNORECASE)
    if match:
        return float(match.group(1))
    return None


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
                        val = val.strip().strip('"').strip("'").strip()
                        os.environ.setdefault(key.strip(), val)
            return
        parent = current.parent
        if parent == current:
            break
        current = parent


_load_dotenv()

try:
    from mistralai.client import Mistral as _Mistral   # mistralai 0.4–0.9
    _MISTRAL_AVAILABLE = True
except ImportError:
    try:
        from mistralai import Mistral as _Mistral      # mistralai >= 1.0
        _MISTRAL_AVAILABLE = True
    except ImportError:
        _MISTRAL_AVAILABLE = False


class MistralAgent(BaseAgent):
    """Agent that calls Mistral AI via the mistralai SDK."""

    def __init__(self, verbose: bool = False):
        super().__init__(verbose)
        self._prompt_count = 0
        if not _MISTRAL_AVAILABLE:
            raise ImportError(
                "mistralai is not installed. Run: pip install mistralai"
            )
        key = os.environ.get("MISTRAL_API_KEY", "")
        if not key or key == "YOUR_MISTRAL_API_KEY_HERE":
            raise ValueError(
                "MISTRAL_API_KEY is not set. Add it to the project-root .env file:\n"
                "  MISTRAL_API_KEY=your_actual_key_here\n"
                "  Get a key at: https://console.mistral.ai/api-keys"
            )
        self._client = _Mistral(api_key=key)

    def fetch_data(self, prompt: str, timeout: int = 120) -> dict:
        return self._call_with_retry(prompt)

    def _call_with_retry(self, prompt: str, _is_retry: bool = False) -> dict:
        if not _is_retry:
            self._prompt_count += 1
        if self.verbose and not _is_retry:
            print(f"\n[MISTRAL | Prompt #{self._prompt_count}] {'─' * 43}")
            print(f"[INPUT]\n{prompt}")
            print(f"{'─' * 60}")

        try:
            response = self._client.chat.complete(
                model=MISTRAL_MODEL,
                messages=[{"role": "user", "content": prompt}],
            )
            output = (response.choices[0].message.content or "").strip()

            if self.verbose:
                print(f"[OUTPUT]\n{output}")
                print(f"{'─' * 60}\n")

            if not output:
                return {"error": "empty response from Mistral"}

            # Direct JSON parse
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                pass

            # Strip markdown fences
            clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", output, flags=re.DOTALL).strip()
            try:
                return json.loads(clean)
            except json.JSONDecodeError:
                pass

            # Last resort: first {...} block
            match = re.search(r"\{.*\}", output, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass

            return {"error": "unparseable response", "raw": output[:300]}

        except Exception as e:
            msg = str(e)
            is_rate_limit = "429" in msg or "rate limit" in msg.lower() or "too many requests" in msg.lower()
            if is_rate_limit and not _is_retry:
                return self._handle_rate_limit(e, prompt)
            return {"error": f"Mistral API error: {e}"}

    def _handle_rate_limit(self, exc: Exception, prompt: str) -> dict:
        print(f"\n[MISTRAL] Rate limited (429 Too Many Requests).")
        delay = _parse_retry_delay(exc)
        if delay is None:
            print("[MISTRAL] Could not parse retry delay from error — giving up.")
            return {"error": f"Mistral API error: {exc}"}

        if delay > RETRY_DELAY_LIMIT:
            print(f"[MISTRAL] retryDelay={delay:.1f}s exceeds {RETRY_DELAY_LIMIT}s limit — TIMEOUT.")
            return {"error": "timeout: rate limit retry delay too long"}

        print(f"[MISTRAL] retryDelay={delay:.1f}s — waiting before retry…")
        for remaining in range(int(delay), 0, -1):
            print(f"\r[MISTRAL] Retrying in {remaining}s…  ", end="", flush=True)
            time.sleep(1)
        print("\r[MISTRAL] Retrying now…              ")
        return self._call_with_retry(prompt, _is_retry=True)

    def display_verbose(self, prompt_input: str, prompt_output: str) -> None:
        print(f"\n[MISTRAL] {'─' * 53}")
        print(f"[INPUT]\n{prompt_input}")
        print(f"{'─' * 60}")
        print(f"[OUTPUT]\n{prompt_output}")
        print(f"{'─' * 60}\n")
