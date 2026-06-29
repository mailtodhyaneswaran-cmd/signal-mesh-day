# Signal Mesh Day — Multi-Strategy Intraday Trading System

An AI-powered intraday trading system. A premarket screener selects the top 3 S&P 500
stocks each morning using Claude + Mistral. A **regime detection layer** scores market
conditions and picks the best execution strategy. Three strategies share the same IBKR
execution machinery and bracket-order risk framework.

> **Paper trading only.** `LIVE_TRADING = False` until validated. Not financial advice.

---

## Project Layout

```
signal-mesh-day/
├── setup_paths.py          sys.path bootstrap + path constants (project root)
│
├── bin/                    Executable entry points
│   ├── day_orchestrator.py Premarket screener: S&P 500 → AI mesh → watchlist
│   ├── live_engine.py      Unified live runner — dispatches ORB / IB / VWAP
│   ├── orb_strategy.py     ORB-only engine (kept for direct use / backward compat)
│   ├── backtest.py         Multi-strategy backtester (--strategy orb|ib|vwap)
│   ├── run_screener.bat    Task Scheduler launcher: 14:55 NL
│   └── run_us.bat          Task Scheduler launcher: 15:30 NL → live_engine.py
│
├── inc/                    Abstract classes / interfaces
│   ├── strategy_base.py    Strategy Protocol + StrategySignal + STRATEGY_REGISTRY
│   └── lib_agents.py       BaseAgent ABC (implemented by Claude / Mistral / Mock)
│
├── lib/                    Concrete strategy + library code
│   ├── orb_core.py         ORB primitives: range, breakout, retest, bracket, simulate
│   ├── ib_strategy.py      60-min Initial Balance breakout (reuses orb_core)
│   ├── vwap_strategy.py    VWAP reversion: compute_vwap, detect_setup, simulate
│   ├── regime.py           Regime scoring → pick_strategy() + fallback chain
│   ├── ibkr_connector.py   ib_async wrapper: connect, bars, RVOL, brackets, EUR/USD
│   ├── telegram_notify.py  HTML Telegram with 429 retry
│   ├── premarket_data.py   Gap scan enrichment (real RVOL via IBKR premarket bars)
│   ├── sp500_universe.py   S&P 500 tickers (Wikipedia scrape, weekly cache)
│   ├── data_loader.py      IBKR 1-min bar cache (RTH .csv + premarket _pm.csv)
│   ├── lib_agents_claude.py  Claude via Claude Code CLI (subprocess, no API key)
│   ├── lib_agents_mistral.py Mistral via mistralai SDK
│   └── lib_agents_gemini.py  Gemini (reserved — inactive)
│
├── tst/                    Tests
│   ├── run_suite.bat       Full system test runner (--mock for fast mode)
│   ├── mock_responses.py   Pre-written AI responses for logic testing (no API calls)
│   ├── test_connection.py  IBKR connection + bracket order round-trip
│   ├── test_rvol.py        Real RVOL calculation + optional AI analysis
│   ├── test_threading.py   Threading / state-lock / EOD flatten (offline)
│   ├── test_integration.py Full pipeline: data → AI → ORB → IBKR order → verify
│   └── test_scenario.py    Regime-aware per-day comparison: ORB vs IB vs VWAP
│
└── dat/                    Data, config, prompts
    ├── config.example.py   Config template — copy to config.py
    ├── config.py           Live config (gitignored — contains credentials)
    ├── day_trading_prompts.py  25 AI prompts (5 bulk calls × 5 categories)
    ├── data/               IBKR cached 1-min bars (gitignored)
    ├── watchlist/          watchlist_YYYYMMDD.json files (gitignored)
    └── results/            Backtest CSV outputs (gitignored)
```

---

## Architecture

```
14:55 NL — Premarket Screener (day_orchestrator.py)
─────────────────────────────────────────────────────
  S&P 500 (503 tickers)
    → batch gap scan (yfinance)
    → hard gates: |gap| >= 1.5%, vol >= $20M
    → top 5 shortlisted
    → IBKR real premarket RVOL (cached _pm.csv)
    → 5 bulk AI calls × Claude + Mistral × 5 tickers
       [price_action | catalyst | sentiment_flow | market_regime | quant_edge]
    → cross-pollination round
    → aggregate_ticker() → top 3 picks
    → regime scoring → "ORB" | "IB" | "VWAP" | "SIT_OUT"
    → watchlist_YYYYMMDD.json  +  Telegram summary

15:30 NL — Live Engine (live_engine.py)
─────────────────────────────────────────────────────
  Reads watchlist (waits up to 10 min if screener still running)
  → strategy field → dispatches:

  ORB  (gap + RVOL days)     wait 15:35, 5-min range, breakout+retest
  IB   (moderate-gap days)   wait 16:30, 60-min range, breakout+retest
  VWAP (flat/choppy days)    starts 15:30, fade VWAP deviation >= 1.5%

  All strategies:
    → bracket order (entry limit / SL / TP)
    → _monitor_bracket() until TP / SL / range re-entry / EOD
    → EOD safety flatten (any leftover positions)
    → Telegram notifications throughout

  Fallback chain: ORB → IB → VWAP → SIT_OUT
    ORB  no trigger by 16:00 NL → try IB
    IB   no trigger by 17:00 NL → try VWAP
    VWAP no edge by   20:00 NL → sit out
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
# ib_async   yfinance   mistralai   lxml
```

Claude uses the **Claude Code CLI** — no API key, authenticated via `claude login`.

### 2. Config

```bash
cp dat/config.example.py dat/config.py
# Fill in:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, MISTRAL_API_KEY
```

### 3. IBKR Paper Account

- Open TWS or IB Gateway in **paper trading** mode, port `7497`
- Start IBKR before 14:55 NL (screener needs premarket RVOL)

```bash
python tst/test_connection.py        # connection + market data
python tst/test_connection.py --eurusd
```

### 4. Task Scheduler (pre-registered)

| Task | Time (NL) | Script |
|------|-----------|--------|
| `SignalMeshDay-Screener` | 14:55 Mon–Fri | `bin/run_screener.bat` |
| `SignalMeshDay-ORB` | 15:30 Mon–Fri | `bin/run_us.bat` → `live_engine.py` |

Logs: `run_screener.log`, `run_us.log` in project root.

---

## Running

### Premarket Screener

```bash
# Full S&P 500 scan → AI mesh → regime → watchlist:
python bin/day_orchestrator.py

# Bypass screener, test specific tickers:
python bin/day_orchestrator.py --tickers NVDA MU TSLA

# Mock AI (no API calls — fast logic test):
python bin/day_orchestrator.py --tickers NVDA MU --mock
```

### Live Engine

```bash
python bin/live_engine.py
# Reads today's watchlist, dispatches ORB/IB/VWAP based on regime field
```

### Backtest

```bash
# ORB (default, 5-min range):
python bin/backtest.py --ticker NVDA --start 2026-06-05 --end 2026-06-24 --no-ibkr

# IB (60-min Initial Balance):
python bin/backtest.py --ticker NVDA --start 2026-06-05 --end 2026-06-24 --strategy ib --no-ibkr

# VWAP reversion:
python bin/backtest.py --ticker NVDA --start 2026-06-05 --end 2026-06-24 --strategy vwap --no-ibkr
```

### Regime Scenario Test (compare all 3 strategies per day)

```bash
python tst/test_scenario.py --ticker NVDA --start 2026-06-05 --end 2026-06-24
python tst/test_scenario.py --ticker MU   --start 2026-06-09 --end 2026-06-24 --ibkr
```

### Full System Test Suite

```bash
# Real mode (~15 min — makes actual Claude + Mistral API calls):
tst\run_suite.bat

# Mock mode (~2-3 min — simulated AI, real IBKR + yfinance):
tst\run_suite.bat --mock
```

Suite runs in order: IBKR connection → yfinance+RVOL → threading → screener → integration.

---

## Strategies

### ORB — 5-min Opening Range Breakout

```
1. 09:30 ET: record H/L of first 5-min candle
2. Watch 1-min bars for close outside range
3. Retest of breakout level → ENTER  |  Retest fail → skip day
4. Bracket: entry at level | SL at opposite edge | TP at 2R
5. No new breakouts after 16:30 NL (too late for 2R before close)
```

### IB — 60-min Initial Balance Breakout

Same logic as ORB but first 60 minutes form the range (09:30–10:30 ET).
No premarket RVOL gate — all signals are post-open.
Fires once per session after 10:30 ET.

### VWAP — VWAP Reversion

```
VWAP = sum((H+L+C)/3 × V) / sum(V), resets daily at 09:30 ET

1. After 15 warmup bars, compute running VWAP
2. When price >= 1.5% away from VWAP + reversal bar confirmed → ENTER
     Price below VWAP → LONG (fade the drop)
     Price above VWAP → SHORT (fade the rally)
3. Target = VWAP level (or 1.5R)  |  Stop = extreme ± ATR
```

### Regime Detection

Scores 4 signals to pick today's strategy:

| Signal | ORB | IB | VWAP |
|--------|-----|----|------|
| Gap > 3% | +2 | +1 | 0 |
| Gap 1–3% | +1 | +2 | 0 |
| Gap < 1% | 0 | 0 | +2 |
| Futures >= 0.5% | +1 | +1 | 0 |
| Futures < 0.3% | 0 | 0 | +2 |
| VIX > 20 | +1 | +1 | 0 |
| VIX < 15 | 0 | 0 | +2 |
| RVOL >= 3x | +2 | +1 | 0 |
| RVOL 1.5–3x | +1 | +2 | 0 |
| RVOL < 1.5x | 0 | 0 | +2 |

Tiebreak preference: **VWAP > IB > ORB** (VWAP most robust to missing data).

---

## AI Prompt Architecture

25 prompts, sent as **5 bulk API calls per agent** (one per category, each returning
a JSON array of 5 results). Total: 10 calls per ticker (2 agents × 5 categories).

| Category | Weight | Focus |
|----------|--------|-------|
| `price_action` | 30% | Gap type, key levels, premarket structure |
| `quant_edge` | 25% | RVOL gate, gap stats, ATR room, risk sizing |
| `catalyst` | 20% | Catalyst type, freshness, direction alignment |
| `sentiment_flow` | 15% | News tone, social velocity, options |
| `market_regime` | 10% | Futures, sector, VIX, macro calendar |

**Aggregation:** Per-agent nets averaged (not all 50 results summed) so the scale
is agent-count-independent. Cross-pollination: if **all** agents revise to NOTHING,
that consensus overrides the round-1 direction.

---

## Real RVOL

```
RVOL = today's premarket volume (04:00–09:30 ET, useRTH=False from IBKR)
     ÷ average premarket volume same window over last 20 trading days
```

Cached in `dat/data/{TICKER}/{DATE}_pm.csv`. After the first screener run, the
20-day baseline loads from disk — no live IBKR needed on repeat runs.

RVOL gate: < 1.5× → hard veto | 1.5–3× → conviction capped 60 | ≥ 3× → full

---

## Capital & Risk

| | |
|--|--|
| House money | €5,000 |
| Risk per trade | 1% ≈ €50 |
| Max concurrent positions | 3 |
| Max trades per symbol/day | 1 |
| ORB / IB TP | 2R |
| VWAP TP | 1.5R |
| Trade currency | USD (converted at live EUR/USD) |

All parameters in `dat/config.py` → `INTRADAY_PARAMS` SimpleNamespace.
Single source of truth for live engine and backtest.

---

## Phase Status

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Foundation: strategy abstraction, ORB core, IBKR/Telegram | ✅ |
| 1 | Premarket AI screener: 5 bulk calls × 2 agents → watchlist | ✅ |
| 2 | Live engine: threading, EOD flatten, state tracking | ✅ |
| 3 | Backtest: ORB / IB / VWAP, metrics, equity curve | ✅ |
| B | Regime detection + IB breakout strategy | ✅ |
| C | VWAP reversion strategy | ✅ |

### Remaining

- [ ] Walk-forward backtest (6+ months data) for IB + VWAP edge validation
- [ ] `live_engine.py` IB/VWAP runners tested live on paper account
- [ ] RVOL message clarity: distinguish Sunday/closed-market from genuine data failure
