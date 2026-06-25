"""
day_orchestrator.py — Signal Mesh Day — Phase 1 AI screener.

Runs premarket (~15:00 NL / ~09:00 ET):
  1. Scan S&P 500 → shortlist 5 candidates by gap + volume score
  2. Enrich each with full premarket data (yfinance)
  3. Run 25 prompts × 2 agents (Claude + Mistral) per ticker
  4. Cross-pollination round (each agent reviews the other's conclusions)
  5. Aggregate votes → top 3 directional picks
  6. Write watchlist_YYYYMMDD.json (read by the ORB engine)
  7. Telegram notifications at each stage

Usage
─────
  # Full run (screener + mesh):
  python day_orchestrator.py

  # Bypass screener — analyse specific tickers:
  python day_orchestrator.py --tickers NVDA TSLA AAPL

  # Show full prompt + response (debug):
  python day_orchestrator.py --tickers NVDA --verbose
"""
import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import config

# Set Mistral API key from config before importing the agent
os.environ.setdefault("MISTRAL_API_KEY", getattr(config, "MISTRAL_API_KEY", ""))

import ibkr_connector
from telegram_notify import send_message
from day_trading_prompts import (
    ALL_INTRADAY_PROMPTS,
    INTRADAY_PARAMS as _DTP_PARAMS,
    CROSS_POLLINATION_PROMPT,
    aggregate_ticker,
    rvol_conviction_cap,
    build_bulk_category_prompt,
)
from sp500_universe import get_sp500_tickers
from premarket_data import fetch_market_context, batch_gap_scan, enrich_ticker
from lib_agents_claude import ClaudeAgent
from lib_agents_mistral import MistralAgent

NL = ZoneInfo("Europe/Amsterdam")

_DIR_TO_SIGNAL = {"LONG": "BUY", "SHORT": "SELL", "NOTHING": "HOLD"}


# ── SafeDict for format_map ───────────────────────────────────────────────────

class _SafeDict(dict):
    """Returns a placeholder for missing keys instead of raising KeyError."""
    def __missing__(self, key: str) -> str:
        return f"[{key}: n/a]"


# ── Agent initialisation ──────────────────────────────────────────────────────

def _build_agents(verbose: bool) -> dict:
    agents = {}
    try:
        agents["claude"] = ClaudeAgent(verbose=verbose)
        print("  Claude  ✓")
    except Exception as e:
        print(f"  Claude  ✗  ({e})")
    try:
        agents["mistral"] = MistralAgent(verbose=verbose)
        print("  Mistral ✓")
    except Exception as e:
        print(f"  Mistral ✗  ({e})")
    if not agents:
        raise RuntimeError("No AI agents available — install/configure at least one.")
    return agents


# ── Round-1: 5 bulk prompts per agent (one per category) ─────────────────────

def _parse_bulk_response(
    raw:         object,
    category:    str,
    prompt_keys: list[str],
) -> list[dict]:
    """
    Parse one agent's response to a bulk category prompt.

    The LLM is asked to return a JSON array of N objects. This function
    normalises whatever comes back into a list of N tagged result dicts.
    Handles: direct list, dict with nested array, single dict, or error.
    """
    import json as _json

    items = None

    if isinstance(raw, list):
        items = raw

    elif isinstance(raw, dict):
        # Check for embedded JSON array in a "raw" error field
        if items is None and "raw" in raw:
            try:
                parsed = _json.loads(raw["raw"])
                if isinstance(parsed, list):
                    items = parsed
            except Exception:
                pass
        # Check common wrapper keys
        if items is None:
            for wrap_key in ("results", "analyses", "data", "items", "answers"):
                if wrap_key in raw and isinstance(raw[wrap_key], list):
                    items = raw[wrap_key]
                    break
        # Propagate hard error to all sub-prompts
        if items is None and "error" in raw:
            return [
                {"signal": "NOTHING", "conviction": 0, "category": category,
                 "prompt_key": k, "error": raw["error"]}
                for k in prompt_keys
            ]
        # Single result dict — wrap as one-element list
        if items is None:
            items = [raw]

    if not items:
        return [
            {"signal": "NOTHING", "conviction": 0, "category": category,
             "prompt_key": k, "error": "empty bulk response"}
            for k in prompt_keys
        ]

    # Map items → prompt keys in order; pad with defaults if LLM returned fewer
    results = []
    for i, key in enumerate(prompt_keys):
        r = dict(items[i]) if i < len(items) and isinstance(items[i], dict) else {}
        r.setdefault("signal",     "NOTHING")
        r.setdefault("conviction", 0)
        r["category"]   = category
        r["prompt_key"] = key
        results.append(r)
    return results


def _run_round1(prompt_data: dict, agents: dict) -> dict[str, list[dict]]:
    """
    Run 5 BULK prompts per agent — one per category, each containing all 5 sub-prompts.
    Returns a JSON array of 5 results per call → 5 API calls × N agents (vs 25 before).

    Returns {agent_name: [25 tagged result dicts]}.
    """
    results: dict[str, list[dict]] = {}
    for agent_name, agent in agents.items():
        agent_results = []
        for category, prompts in ALL_INTRADAY_PROMPTS.items():
            # Pre-fill every template with real data
            filled = [
                (key, template.format_map(_SafeDict(prompt_data)))
                for key, template in prompts.items()
            ]
            # One API call per category
            bulk_prompt = build_bulk_category_prompt(category, filled)
            raw         = agent.fetch_data(bulk_prompt)
            # Parse the JSON array of 5 back into individual result dicts
            parsed = _parse_bulk_response(raw, category, list(prompts.keys()))
            for r in parsed:
                r["agent"] = agent_name
            agent_results.extend(parsed)

        results[agent_name] = agent_results
        wins = sum(1 for r in agent_results if "error" not in r)
        print(f"    [{agent_name}] {wins}/{len(agent_results)} results OK "
              f"(5 bulk calls)")
    return results


# ── Cross-pollination ─────────────────────────────────────────────────────────

def _summarise_agent(agent_name: str, results: list[dict]) -> str:
    """Build a per-category summary string for the cross-pollination prompt."""
    by_cat: dict[str, list[dict]] = {}
    for r in results:
        by_cat.setdefault(r.get("category", "unknown"), []).append(r)

    lines = [f"{agent_name} conclusions:"]
    for cat, items in by_cat.items():
        signals   = [r.get("signal", "NOTHING") for r in items]
        dominant  = max(set(signals), key=signals.count)
        avg_conv  = int(sum(r.get("conviction", 0) for r in items) / len(items))
        top_reason = next(
            (r.get("reasoning", "") for r in items if r.get("signal") == dominant), ""
        )[:120]
        lines.append(f"  {cat}: {dominant} ({avg_conv}%) — {top_reason}")
    return "\n".join(lines)


def _run_cross_pollination(
    prompt_data:  dict,
    round1:       dict[str, list[dict]],
    agents:       dict,
) -> dict[str, dict]:
    """
    Each agent sees the other agent's per-category summary and may revise.
    Returns {agent_name: revised_result_dict}.
    """
    summaries = {
        name: _summarise_agent(name, results)
        for name, results in round1.items()
    }
    revised: dict[str, dict] = {}
    for agent_name, agent in agents.items():
        cp_data = {
            **prompt_data,
            "agent_name":    agent_name,
            "own_summary":   summaries[agent_name],
            "peer_summaries": "\n\n".join(
                s for n, s in summaries.items() if n != agent_name
            ),
        }
        filled = CROSS_POLLINATION_PROMPT.format_map(_SafeDict(cp_data))
        r      = agent.fetch_data(filled)
        r["agent"] = agent_name
        revised[agent_name] = r
        delta = r.get("confidence_delta", 0)
        print(f"    [{agent_name}] cross-poll → {r.get('revised_signal','?')} (Δ{delta:+})")
    return revised


# ── Aggregation ───────────────────────────────────────────────────────────────

def _aggregate(round1: dict[str, list[dict]], rvol: float) -> dict:
    """Flatten all round-1 prompt results and call aggregate_ticker."""
    all_results = [r for results in round1.values() for r in results]
    return aggregate_ticker(all_results, rvol, _DTP_PARAMS)


# ── Watchlist writer ──────────────────────────────────────────────────────────

def _write_watchlist(picks: list[dict], eurusd: float) -> Path:
    """Write watchlist_YYYYMMDD.json per Appendix A schema."""
    today     = datetime.now(NL).strftime("%Y%m%d")
    now_utc   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    usd_budget = round(config.HOUSE_MONEY_EUR * eurusd, 2)

    doc = {
        "date":             datetime.now(NL).strftime("%Y-%m-%d"),
        "generated_at_utc": now_utc,
        "eur_usd_rate":     eurusd,
        "house_money_eur":  config.HOUSE_MONEY_EUR,
        "usd_budget":       usd_budget,
        "picks":            picks,
    }
    out_dir = Path(config.WATCHLIST_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"watchlist_{today}.json"
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"\n  Watchlist → {path}")
    return path


# ── Telegram helpers ──────────────────────────────────────────────────────────

def _tg_shortlist(candidates: list[dict]) -> None:
    icon = lambda g: "🟢" if g > 0 else "🔴"
    lines = ["<b>📋 Signal Mesh Day — shortlist for analysis</b>"]
    for c in candidates:
        lines.append(f"{icon(c['gap_pct'])} <b>{c['ticker']}</b>  gap {c['gap_pct']:+.1f}%")
    send_message("\n".join(lines))


def _tg_ticker_result(ticker: str, direction: str, conviction: float,
                       net: float, rvol: float, catalyst: str) -> None:
    icons = {"LONG": "📈", "SHORT": "📉", "NOTHING": "😴"}
    send_message(
        f"{icons.get(direction,'❓')} <b>{ticker}</b> → <b>{direction}</b>\n"
        f"  Conviction {conviction:.0%}  |  net {net:+.4f}  |  RVOL {rvol:.1f}x\n"
        f"  {catalyst[:100]}"
    )


def _tg_watchlist_summary(picks: list[dict]) -> None:
    if not picks:
        send_message("😴 <b>Day screener</b>: no actionable picks today.")
        return
    lines = ["<b>✅ Watchlist written — picks for ORB engine:</b>"]
    for p in picks:
        icon = "📈" if p["direction"] == "long" else "📉"
        lines.append(
            f"{icon} <b>{p['ticker']}</b>  {p['direction'].upper()}"
            f"  conf {p['confidence']:.0%}  RVOL {p['rvol']:.1f}x"
        )
    send_message("\n".join(lines))


# ── Main orchestration ────────────────────────────────────────────────────────

def run(
    tickers_override: list[str] | None = None,
    verbose: bool = False,
) -> list[dict]:
    """
    Full Phase 1 orchestration. Returns the picks list written to the watchlist.

    Args:
        tickers_override: if set, skip the screener and use these tickers.
        verbose: print full prompt + response for every AI call.
    """
    params = config.INTRADAY_PARAMS
    print(f"\n{'='*58}")
    print(f"  Signal Mesh Day — AI Screener  "
          f"{datetime.now(NL).strftime('%Y-%m-%d %H:%M')} NL")
    print(f"{'='*58}\n")

    # ── EUR/USD rate ───────────────────────────────────────────────────────
    try:
        eurusd = ibkr_connector.get_eurusd_rate()
    except Exception:
        eurusd = 1.08
    print(f"  EUR/USD {eurusd:.4f}  |  USD budget "
          f"${config.HOUSE_MONEY_EUR * eurusd:,.0f}\n")

    # ── AI agents ──────────────────────────────────────────────────────────
    print("  Initialising AI agents...")
    agents = _build_agents(verbose)

    # ── Market context ─────────────────────────────────────────────────────
    print("\n  Fetching market context...")
    mkt_ctx = fetch_market_context()
    print(f"  SPY {mkt_ctx['spy_premarket_pct']}  "
          f"QQQ {mkt_ctx['qqq_premarket_pct']}  "
          f"VIX {mkt_ctx['vix']} ({mkt_ctx['vix_change']})")

    # ── Screener funnel ────────────────────────────────────────────────────
    if tickers_override:
        print(f"\n  Bypassing screener → {tickers_override}")
        candidates = [
            {"ticker": t, "gap_pct": 0.0, "premarket_price": 0.0, "avg_volume": 0}
            for t in tickers_override
        ]
    else:
        print("\n  Loading S&P 500 universe...")
        sp500 = get_sp500_tickers()
        print(f"\n  Running gap scan ({len(sp500)} tickers)...")
        raw = batch_gap_scan(sp500, params)
        if not raw:
            print("  No candidates — market may be closed or flat.")
            return []
        # Sector dedup: max_per_sector — skip for now (no sector in batch scan)
        candidates = raw[: params.shortlist_size]

    _tg_shortlist(candidates)
    print(f"\n  Shortlist: {', '.join(c['ticker'] for c in candidates)}\n")

    # ── Per-ticker mesh ────────────────────────────────────────────────────
    picks: list[dict] = []

    for i, cand in enumerate(candidates, 1):
        ticker = cand["ticker"]
        print(f"  ── [{i}/{len(candidates)}] {ticker} {'─'*42}")

        # Full data enrichment
        print(f"  Enriching premarket data...")
        data = enrich_ticker(ticker, mkt_ctx, candidates, eurusd, params)
        rvol = float(data.get("rvol_premarket", 0))

        # Hard RVOL veto before running expensive prompts
        cap = rvol_conviction_cap(rvol, _DTP_PARAMS)
        if cap == 0:
            print(f"  ⛔ RVOL {rvol:.1f}x < {params.rvol_hard_floor}x — veto, skipping")
            send_message(f"⛔ <b>{ticker}</b>: RVOL {rvol:.1f}x below floor — skipped")
            continue

        print(f"  RVOL {rvol:.1f}x (cap={cap})  "
              f"gap {data['premarket_gap_pct']}%  "
              f"catalyst: {data['catalyst_summary'][:60]}")

        # Round 1: 25 prompts × N agents
        print(f"  Running 25 prompts × {len(agents)} agents...")
        round1 = _run_round1(data, agents)

        # Cross-pollination
        print(f"  Cross-pollination...")
        _run_cross_pollination(data, round1, agents)

        # Aggregate
        agg       = _aggregate(round1, rvol)
        direction = agg["direction"]   # "LONG" | "SHORT" | "NOTHING"
        net       = agg["net"]
        # Conviction: scale |net| against max possible (0.5 is a strong signal)
        conviction = min(abs(net) / 0.5, 1.0)

        print(f"  → {direction}  net={net:+.4f}  conviction={conviction:.0%}")
        _tg_ticker_result(ticker, direction, conviction, net, rvol,
                          data.get("catalyst_summary", ""))

        if direction == "NOTHING":
            continue

        picks.append({
            "ticker":           ticker,
            "signal":           _DIR_TO_SIGNAL[direction],
            "direction":        direction.lower(),
            "currency":         data.get("currency", "USD"),
            "rvol":             rvol,
            "catalyst":         data.get("catalyst_summary", ""),
            "confidence":       round(conviction, 4),
            "reasoning":        (
                f"net={net:+.4f}  RVOL={rvol:.1f}x  "
                f"gap={data['premarket_gap_pct']}%"
            ),
            "avg_daily_volume": int(data.get("avg_daily_volume") or 0),
            "last_price":       float(data.get("premarket_price") or 0),
            "sector":           data.get("sector", ""),
        })

    # ── Sort by confidence, keep top MAX_CONCURRENT_POSITIONS ─────────────
    picks.sort(key=lambda x: x["confidence"], reverse=True)
    picks = picks[: config.MAX_CONCURRENT_POSITIONS]

    # ── Write watchlist + notify ───────────────────────────────────────────
    _write_watchlist(picks, eurusd)
    _tg_watchlist_summary(picks)

    print(f"\n  Done — {len(picks)} pick(s) in today's watchlist.")
    return picks


def main() -> None:
    p = argparse.ArgumentParser(
        description="Signal Mesh Day — premarket AI screener (Phase 1)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--tickers", nargs="+", metavar="T",
                   help="Bypass screener, analyse these tickers")
    p.add_argument("--verbose", action="store_true",
                   help="Print every prompt and AI response")
    args = p.parse_args()
    run(tickers_override=args.tickers, verbose=args.verbose)


if __name__ == "__main__":
    main()
