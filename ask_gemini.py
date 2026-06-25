#!/usr/bin/env python3
"""
ask_gemini.py — CLI wrapper around GeminiAgent.

Usage:
    python ask_gemini.py "Your prompt here"
    python ask_gemini.py          # reads prompt interactively from stdin
    echo "Your prompt" | python ask_gemini.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib_agents_gemini import GeminiAgent


def main() -> None:
    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:])
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
    else:
        print("Enter your prompt (press Ctrl+D when done):")
        try:
            prompt = sys.stdin.read().strip()
        except KeyboardInterrupt:
            print("\nAborted.")
            sys.exit(0)

    if not prompt:
        print("Error: no prompt provided.", file=sys.stderr)
        sys.exit(1)

    try:
        agent = GeminiAgent(verbose=True)
    except (ImportError, ValueError) as e:
        print(f"Setup error: {e}", file=sys.stderr)
        sys.exit(1)

    result = agent.fetch_data(prompt)

    if "error" in result:
        if result["error"] == "unparseable response":
            # Raw text was already printed by display_verbose — nothing more to do
            pass
        else:
            print(f"\n[ERROR] {result['error']}", file=sys.stderr)
            sys.exit(1)
    else:
        print("[PARSED JSON]")
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
