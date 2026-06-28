# Signal Mesh Day — Multi-Strategy Intraday Trading System

An AI-powered intraday trading system. A premarket screener selects the top 3 S&P 500
stocks each morning. A **regime detection layer** scores market conditions and picks the
best execution strategy for the day. Three strategies share the same IBKR execution
machinery and bracket-order risk framework.

> **Paper trading only.** `LIVE_TRADING = False` until validated. Not financial advice.

---

## Architecture

```
┌───────────────────────────────────────────────────��─────────────┐
│  Phase 1 — Premarket Screener  (~15:00 NL / 09:00 ET)          │
│                                                                 │
│  S&P 500 (503 tickers)                                          │
│       ↓  batch gap scan (yfinance)                              │
│  Top 30 movers  →  hard gates (gap ≥ 1.5%, vol ≥ $20M)        │
│       ↓                                                         │
│  Top 5 shortlisted                                              │
│       ↓  enrich (IBKR real premarket RVOL + yfinance ATR/news) │
│  5 bulk prompts × Claude + Mistral × 5 tickers                 │
│  [5 category calls per agent = 25 analyses, 10 API calls total] │
│       ↓  cross-pollination → aggregate_ticker()                 │
│  Top 3 picks → watchlist_YYYYMMDD.json                         │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│  Regime Detection  (runs at screener end)                        │
│                                                                 │
│  Score 4 signals: gap size, futures direction, VIX, RVOL       │
│       ↓  pick_strategy()                                        │
│  "ORB" | "IB" | "VWAP" | "SIT_OUT"                            │
│  Written to watchlist.json  →  read by live engine             │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│  Phase 2 — Execution Engine  (~15:30 NL / 09:30 ET)            │
│                                                                 │
│  Read watchlist → strategy + picks                              │
│                                                                 │
│  ORB  (gap + RVOL days)                                        │
│    First 5-min candle → watch 1-min bars → breakout + retest   │
│    Bracket: entry / SL at range edge / TP at 2R               │
│                                                                 │
│  IB   (moderate-gap days, no premarket RVOL needed)            │
│    First 60-min candle (9:30–10:30 ET) → breakout + retest     │
│    Same bracket logic as ORB                                    │
│                                                                 │
│  VWAP (flat/choppy days, no premarket data needed)             │
│    Running VWAP from open → fade price when ≥1.5% away        │
│    Entry on reversal bar, target at VWAP, stop beyond extreme  │
│                                                                 │
│  Fallback chain: ORB → IB → VWAP → SIT_OUT                    │
│  Monitor → TP / SL / re-entry exit / EOD flatten              │
└─────────────────────────────────────────────────────────────────┘
```

---

## File Structure

```
signal-mesh-day/
│
│  ── Strategies ─────────────────────────────────────────────────
├── strategy_base.py        Protocol interface every strategy implements
├── orb_core.py             Shared ORB primitives (range, breakout, retest, bracket)
├── orb_strategy.py         Live ORB engine (loads watchlist, polls IBKR, places orders)
├── ib_strategy.py          60-min Initial Balance breakout (reuses orb_core)
├── vwap_strategy.py        VWAP reversion (compute_vwap, detect_setup, simulate)
├── regime.py               Regime scoring → pick_strategy() + fallback chain
│
│  ── Phase 1: Screener ──────────────────────────────────────────
├── sp500_universe.py       S&P 500 tickers (Wikipedia scrape, weekly cache)
├── premarket_data.py       Gap scan + per-ticker enrichment (real RVOL via IBKR)
├── day_trading_prompts.py  25 AI prompts (5 categories × 5) + aggregation logic
├── day_orchestrator.py     Screener → mesh → regime pick → watchlist
│
│  ── Backtest & Testing ─────────────────────────────────────────
├── data_loader.py          IBKR 1-min bar cache (RTH + premarket _pm.csv)
├── backtest.py             Multi-strategy backtester (--strategy orb|ib|vwap)
├── test_scenario.py        Regime-aware scenario: compares all 3 strategies per day
├── test_rvol.py            Tests real RVOL calculation + optional AI analysis
├── test_integration.py     Full pipeline: data → AI → ORB → IBKR order → verify
├── test_connection.py      IBKR connection + bracket order round-trip
│
│  ── AI Agents ──────────────────────────────────────────────────
├── lib_agents.py           Abstract BaseAgent interface
├── lib_agents_claude.py    Claude via Claude Code CLI (subprocess, no API key)
├── lib_agents_mistral.py   Mistral via mistralai SDK
├── lib_agents_gemini.py    Gemini (reserved — inactive)
│
│  ── Infrastructure ─────────────────────────────────────────────
├── ibkr_connector.py       ib_async wrapper: connect, bars, RVOL, brackets, EUR/USD
│                           + get_premarket_volume_ibkr() / get_avg_premarket_volume_ibkr()
├── telegram_notify.py      HTML Telegram notifications with 429 retry
├── config.example.py       Config template — copy to config.py
│
│  ── Launchers (Task Scheduler) ─────────────────────────────────
├── run_screener.bat        Runs day_orchestrator.py at 14:55 NL (premarket)
├── run_us.bat              Runs orb_strategy.py at 15:30 NL (US open)
│
│  ── Reference ──────────────────────────────────────────────────
├── todo.md                 Full project spec and phase roadmap
├── todo_master.md          Strategy context + regime design (project owner notes)
└── 930_candle_strategy_algorithm.md  ORB algorithm reference
```

---

## Setup

### 1. Dependencies

```bash
pip install -r requirements.txt
```

```
ib_async      # IBKR API
yfinance      # Market data
mistralai     # Mistral AI SDK
lxml          # pandas.read_html (S&P 500 scrape)
```

Claude uses the **Claude Code CLI** — no API key needed, authenticated via `claude login`.

### 2. Config

```bash
cp config.example.py config.py
# Fill in:
#   TELEGRAM_BOT_TOKEN   — from @BotFather
#   TELEGRAM_CHAT_ID     — your Telegram chat ID
#   MISTRAL_API_KEY      — from console.mistral.ai
```

### 3. IBKR Paper Account

- Open TWS or IB Gateway in **paper trading** mode
- Port: `7497` (TWS paper) or `4001` (Gateway paper)
- Start IBKR at **15:00 NL** (before the screener fires at 15:00 NL)

```bash
python test_connection.py          # connection + contract + bars
python test_connection.py --eurusd # live EUR/USD rate
```

### 4. Windows Task Scheduler (auto-registered)

Two tasks are already registered:

| Task | Time | Script |
|------|------|--------|
| `SignalMeshDay-Screener` | 14:55 NL Mon–Fri | `run_screener.bat` → `day_orchestrator.py` |
| `SignalMeshDay-ORB`      | 15:30 NL Mon–Fri | `run_us.bat` → `orb_strategy.py` |

Logs: `run_screener.log`, `run_us.log` in the project folder.

---

## Running

### Premarket Screener (Phase 1)

Runs automatically at 14:55 NL via Task Scheduler. To run manually:

```bash
# Full S&P 500 scan → top 5 → AI mesh → regime pick → watchlist:
python day_orchestrator.py

# Bypass screener, analyse specific tickers:
python day_orchestrator.py --tickers NVDA TSLA

# Print every AI prompt + response:
python day_orchestrator.py --tickers NVDA --verbose
```

**Output:** `watchlist/watchlist_YYYYMMDD.json` + Telegram notifications.

The watchlist now includes the strategy selected by regime detection:
```json
{
  "strategy": "IB",
  "fallback_window": "17:00",
  "picks": [...]
}
```

### ORB Engine (Phase 2)

Runs automatically at 15:30 NL via Task Scheduler. To run manually:

```bash
python orb_strategy.py
```

Reads today's watchlist, waits for the 09:30 candle, polls 1-min bars for
breakout + retest, places bracket orders, monitors until EOD flatten.

### Backtest (Phase 3)

```bash
# ORB — 5-min Opening Range Breakout (default):
python backtest.py --ticker NVDA --start 2026-06-05 --end 2026-06-24 --no-ibkr

# IB — 60-min Initial Balance Breakout:
python backtest.py --ticker NVDA --start 2026-06-05 --end 2026-06-24 --strategy ib --no-ibkr

# VWAP — VWAP Reversion:
python backtest.py --ticker NVDA --start 2026-06-05 --end 2026-06-24 --strategy vwap --no-ibkr

# Other cached tickers:
python backtest.py --ticker MU   --start 2026-06-09 --end 2026-06-24 --strategy ib   --no-ibkr
python backtest.py --ticker GLW  --start 2026-06-09 --end 2026-06-24 --strategy vwap --no-ibkr

# Per-bar trace for one day:
python backtest.py --ticker NVDA --start 2026-06-10 --end 2026-06-10 --strategy vwap --no-ibkr --verbose
```

**Strategy flags:**

| `--strategy` | Range | RVOL needed | Best on |
|-------------|-------|-------------|---------|
| `orb` (default) | 5-min | Yes (premarket) | High-gap, high-RVOL days |
| `ib` | 60-min | No | Moderate-gap, directional days |
| `vwap` | Continuous | No | Flat/choppy days, price oscillates |

**Bias flags:** `--bias auto` (default), `mechanical`, `long`, `short`

### Regime Scenario Test

Compares all 3 strategies side-by-side per day and shows whether regime-adaptive
selection beats always running one strategy:

```bash
# NVDA — all cached data:
python test_scenario.py --ticker NVDA --start 2026-06-05 --end 2026-06-24

# Fetch RTH data for non-NVDA tickers (needs IBKR), then run:
python test_scenario.py --ticker MU --start 2026-06-09 --end 2026-06-24 --ibkr
python test_scenario.py --ticker MU --start 2026-06-09 --end 2026-06-24  # cache-only after

# With historical SPY/VIX from yfinance for richer regime scoring:
python test_scenario.py --ticker NVDA --start 2026-06-05 --end 2026-06-24 --live-context
```

**Sample output:**
```
Date         Gap%    RVOL  Days  Regime │        ORB │         IB │       VWAP │     Regime
2026-06-05  +2.3%   2.1x   4d   IB     │  -$50 (sl) │  +$102 (tp)│   -$32 (sl)│  +$102
2026-06-08  -0.8%   0.9x   5d   VWAP   │  no setup  │  no setup  │   +$45 (vw)│   +$45
──────────────────────────────────────────────────────────────────────────────────────────
Always ORB:         8 trades  38% WR  net -$120
Always IB:         11 trades  55% WR  net +$340
Always VWAP:       14 trades  57% WR  net +$520
Regime-adaptive:   13 trades  54% WR  net +$380
```

### RVOL Test

```bash
# Test RVOL calculation only (~20 s, no AI):
python test_rvol.py --ticker NVDA

# RVOL + full AI analysis:
python test_rvol.py --ticker NVDA --ai
```

### Integration Test

```bash
# Full pipeline: data → AI → ORB → IBKR bracket order (9 steps):
python test_integration.py --ticker NVDA --date 2026-06-24

# Fast (skip AI — tests data + ORB + order placement only):
python test_integration.py --ticker NVDA --date 2026-06-24 --skip-ai

# Leave orders open to inspect in TWS:
python test_integration.py --ticker NVDA --date 2026-06-24 --skip-ai --no-cancel
```

---

## Strategy Logic

### ORB — Opening Range Breakout

```
1. 09:30 ET: record HIGH / LOW of the first 5-min candle
2. Watch 1-min bars for a CLOSE outside the range
3. Wait for RETEST of the breakout level:
     LONG  — bar low touches range high, closes above → ENTER
     SHORT — bar high touches range low, closes below  → ENTER
     Failed retest (wrong side) → skip today
4. Bracket: entry at breakout level | SL at opposite edge | TP at 2R
5. Exits: TP / SL / range re-entry / 16:30 NL cutoff / EOD flatten
```

### IB — Initial Balance Breakout

```
1. 09:30–10:30 ET: record HIGH / LOW of the first 60-min candle
2. After 10:30: same breakout + retest logic as ORB
3. No premarket RVOL gate — all signals are post-open
4. Wider stop (60-min range edge) → fewer shares, same dollar risk
```

### VWAP — VWAP Reversion

```
VWAP = sum((H+L+C)/3 × V) / sum(V), resets at 09:30 ET daily

1. After 15 warmup bars, compute running VWAP
2. When price closes ≥ 1.5% away from VWAP:
     Price below VWAP → fade down, go LONG
     Price above VWAP → fade up, go SHORT
3. Optional reversal candle confirmation
4. Target: VWAP level (or 1.5R if VWAP is too close)
5. Stop: recent extreme ± 1× ATR of bar range
```

### Regime Detection

Scores 4 premarket signals to pick the best strategy:

| Signal | ORB | IB | VWAP |
|--------|-----|----|------|
| Gap > 3% | +2 | +1 | 0 |
| Gap 1–3% | +1 | +2 | 0 |
| Gap < 1% | 0 | 0 | +2 |
| Futures dir. ≥ 0.5% | +1 | +1 | 0 |
| Futures flat < 0.3% | 0 | 0 | +2 |
| VIX > 20 | +1 | +1 | 0 |
| VIX < 15 | 0 | 0 | +2 |
| RVOL ≥ 3× | +2 | +1 | 0 |
| RVOL 1.5–3× | +1 | +2 | 0 |
| RVOL < 1.5× | 0 | 0 | +2 |

Highest score wins. Tiebreak preference: **VWAP > IB > ORB** (VWAP is most robust
to data failures).

**Runtime fallback chain (if chosen strategy doesn't trigger):**
```
ORB  → no trigger by 16:00 NL (10:00 ET) → try IB
IB   → no trigger by 17:00 NL (11:00 ET) → try VWAP
VWAP → no edge by   20:00 NL (14:00 ET)  → sit out
```

---

## Real RVOL (IBKR premarket bars)

RVOL is computed from actual IBKR premarket bars — not estimated from daily volume:

```
RVOL = today's premarket volume (04:00–09:30 ET, useRTH=False)
     ÷ average premarket volume same window over last 20 trading days
```

**Cache:** `data/{ticker}/{date}_pm.csv` — each day's premarket bars are cached
permanently after first fetch. The 20-day baseline loads from cached files with no
live IBKR connection needed on repeat runs.

---

## RVOL Gate (tiered)

| Premarket RVOL | Screener effect | ORB live gate |
|----------------|-----------------|---------------|
| < 1.5× | Hard NOTHING veto — skip prompts | Skip breakout |
| 1.5× – 3× | Run prompts, conviction capped at 60 | Pass |
| ≥ 3× | Full conviction | Pass |

IB and VWAP have no premarket RVOL gate — all signals are post-open.

---

## AI Prompt Architecture

25 prompts across 5 categories, sent as **5 bulk calls per agent** (one per category,
each containing all 5 sub-prompts → JSON array of 5 results). Total: 10 API calls
per ticker vs 50 individual calls previously.

| Category | Weight | Focus |
|----------|--------|-------|
| `price_action` | 30% | Gap type, key levels, premarket structure, open scenario |
| `quant_edge` | 25% | RVOL gate, gap stats, ATR room, risk sizing, setup grade |
| `catalyst` | 20% | Catalyst type, freshness, direction alignment, event risk |
| `sentiment_flow` | 15% | News tone, social velocity, options positioning |
| `market_regime` | 10% | Futures, sector, VIX, macro calendar |

Each prompt returns `LONG / SHORT / NOTHING` + conviction (0–100).

**Aggregation:**
1. Per-agent nets averaged (not all 50 results summed — keeps scale agent-count-independent)
2. Cross-pollination round: if **all** agents revise to NOTHING, that consensus overrides the round-1 direction

---

## Capital & Risk

| Parameter | Value |
|-----------|-------|
| House money | €5,000 EUR |
| Risk per trade | 1% (≈ €50) |
| Max concurrent positions | 3 |
| Max trades per symbol/day | 1 |
| Trade currency | USD (converted at live EUR/USD from yfinance) |
| ORB / IB TP | 2R |
| VWAP TP | 1.5R (more conservative — target is VWAP, not a fixed level) |

All parameters in `config.INTRADAY_PARAMS` — single source of truth for live engine
and backtest.

---

## Phase Status

| Phase | Description | Status |
|-------|-------------|--------|
| **0** | Foundation: strategy abstraction, ORB core, IBKR/Telegram adapters | ✅ Done |
| **1** | Premarket AI screener → watchlist (5 bulk prompts × 2 agents) | ✅ Done |
| **2** | Live ORB engine (sequential, no threading yet) | 🔧 Partial |
| **3** | Backtest harness: ORB / IB / VWAP, metrics, equity curve | ✅ Done |
| **B** | Regime detection + IB strategy | ✅ Done |
| **C** | VWAP reversion strategy | ✅ Done |

### Remaining items

- [ ] Multi-ticker threading (`threading.Thread` per pick in `orb_strategy.py`)
- [ ] Hard EOD flatten fallback for all open positions
- [ ] Extend backtest to 6+ months for IB + VWAP edge validation
- [ ] Walk-forward out-of-sample split
