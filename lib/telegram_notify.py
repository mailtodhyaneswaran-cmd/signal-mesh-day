"""
telegram_notify.py — Telegram notification helper for Signal Mesh Day.

Uses stdlib urllib (no requests dep) with 429 retry/backoff, matching
the signal_mesh style.  Sends in HTML parse mode.
"""
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import config


def send_message(text: str) -> None:
    """Send an HTML-formatted message to the configured Telegram chat."""
    if (not config.TELEGRAM_BOT_TOKEN
            or "YOUR_" in config.TELEGRAM_BOT_TOKEN
            or not config.TELEGRAM_CHAT_ID
            or "YOUR_" in config.TELEGRAM_CHAT_ID):
        print(f"[Telegram disabled] {text}")
        return

    url    = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    params = {
        "chat_id":    config.TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
    }
    _send_with_retry(url, params)


def _send_with_retry(url: str, params: dict, max_attempts: int = 3) -> None:
    for attempt in range(max_attempts):
        try:
            data = urllib.parse.urlencode(params).encode()
            req  = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode())
            if not body.get("ok"):
                print(f"[Telegram] API returned ok=false: {body}")
            return
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = json.loads(e.read().decode()).get("parameters", {}).get(
                        "retry_after", 2 ** attempt
                    )
                except Exception:
                    wait = 2 ** attempt
                print(f"[Telegram] Rate limited (429), retrying in {wait}s...")
                if attempt < max_attempts - 1:
                    time.sleep(wait)
                    continue
            print(f"[Telegram] HTTP error {e.code}: {e}")
            if attempt < max_attempts - 1:
                time.sleep(2 ** attempt)
        except Exception as e:
            print(f"[Telegram] Send error: {e}")
            if attempt < max_attempts - 1:
                time.sleep(2 ** attempt)
    print("[Telegram] Failed after max retries — continuing without notification.")
