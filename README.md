# Signal Mesh Day — Intraday ORB Day-Trading System

An AI-powered intraday trading system built on top of Signal Mesh. The existing swing-horizon mesh acts as a **premarket screener and directional bias generator**. A separate IBKR engine executes a **5-minute Opening Range Breakout (ORB)** on the top 3 picks, sets bracket orders (TP/SL), and flattens everything by the US close.

> **Paper trading only.** `LIVE_TRADING = False` until validated. Not financial advice.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Phase 1 — Premarket Screener  (~15:00 NL / 09:00 ET)       │
│                                                             │
│  S&P 500 universe (500 tickers)                             │
│       ↓  batch gap scan (yfinance)                          │
│  Top 30 movers  →  hard gates (gap ≥ 1.5%, vol ≥ $20M)    │
│       ↓                                                     │
│  Top 5 shortlisted                                          │
│       ↓  enrich per ticker (yfinance: ATR, RVOL, news)     │
│  25 prompts × Claude + Mistral  ×  5 tickers               │
│       ↓  cross-pollination  →  aggregate_ticker()           │
│  Top 3 picks  →  watchlist_YYYYMMDD.json                   │
│                     (direction: long / short / skip)        │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│  Phase 2 — ORB Engine  (~15:30 NL / 09:30 ET)              │
│                                                             │
│  Read watchlist → keep long / short tickers                 │
│       ↓  wait for 09:30 ET candle to close                 │
│  Opening range = first 5-min candle H/L                     │
│       ↓  watch 1-min bars (IBKR reqHistoricalData)         │
│  Breakout + retest confirmed  →  place bracket order        │
│       (entry limit / SL / TP at 2R)                        │
│       ↓                                                     │
│  Monitor: TP hit / SL hit / re-entry exit / EOD flatten     │
└─────────────────────────────────────────────────────────────┘
```

---

## File Structure

```
signal-mesh-day/
│
│  ── Core strategy ──────────────────────────────────────────
├── strategy_base.py          Protocol interface every strategy implements
├── orb_core.py               Shared ORB primitives (range, breakout, retest, bracket)
├── orb_strategy.py           Live ORB engine — loads watchlist, polls IBKR, places orders
│
│  ── Phase 1: Screener ──────────────────────────────────────
├── sp500_universe.py         S&P 500 ticker list (Wikipedia, weekly cache)
├── premarket_data.py         Batch gap scan + per-ticker enrichment (yfinance)
├── day_trading_prompts.py    25 AI prompts (5 categories × 5) + aggregation logic
├── day_orchestrator.py       Phase 1 orchestrator: screener → mesh → watchlist
│
│  ── Phase 3: Backtest ──────────────────────────────────────
├── data_loader.py            IBKR 1-min bar fetcher with local CSV cache
├── backtest.py               ORB backtester using orb_core.simulate_session()
│
│  ── AI agents ──────────────────────────────────────────────
├── lib_agents.py             Abstract BaseAgent interface
├── lib_agents_claude.py      Claude via Claude Code CLI (subprocess)
├── lib_agents_mistral.py     Mistral via mistralai SDK
├── lib_agents_gemini.py      Gemini (reserved — currently inactive)
│
│  ── Infrastructure ─────────────────────────────────────────
├── ibkr_connector.py         ib_async wrapper (connect, bars, bracket orders, EUR/USD)
├── telegram_notify.py        Telegram HTML notifications with 429 retry
├── config.example.py         Config template — copy to config.py and fill credentials
│
│  ── Tests ──────────────────────────────────────────────────
├── test_connection.py        IBKR connection + bracket order round-trip test
├── test_integration.py       Full pipeline test: data → AI → ORB → IBKR order → verify
│
│  ── Reference ──────────────────────────────────────────────
├── todo.md                   Full project spec and phase roadmap
├── 930_candle_strategy_algorithm.md  ORB strategy algorithm reference
└── run_us.bat                Windows launcher for the ORB engine (Task Scheduler)
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

Claude uses the **Claude Code CLI** (no API key — authenticated via `claude` CLI login).

### 2. Config

```bash
cp config.example.py config.py
# Edit config.py and fill in:
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_CHAT_ID
#   MISTRAL_API_KEY
```

### 3. IBKR Paper Account

- Open TWS or IB Gateway in **paper trading** mode
- Port: `7497` (TWS paper) or `4001` (Gateway paper)
- Verify connection:

```bash
python test_connection.py
python test_connection.py --eurusd
```

---

## Running

### Backtest (Phase 3 — start here)

Prove mechanical edge before wiring anything live.

```bash
# Fetch data from IBKR + run ORB backtest (true ORB: first breakout wins):
python backtest.py --ticker NVDA --start 2025-01-01 --end 2025-06-01

# Cache-only (data already fetched):
python backtest.py --ticker NVDA --start 2025-01-01 --end 2025-06-01 --no-ibkr

# Force direction / verbose per-bar trace:
python backtest.py --ticker NVDA --start 2025-01-01 --end 2025-06-01 --bias long --verbose
```

**Bias modes:**

| Flag | Behaviour |
|------|-----------|
| `--bias auto` | True ORB — first breakout (either side) wins. **Default.** |
| `--bias long` | Always long — isolates one side |
| `--bias short` | Always short |
| `--bias mechanical` | Pre-set from opening candle colour (green=long, red=short) |

### Premarket Screener (Phase 1)

Run at **~15:00 NL / 09:00 ET** (before the open).

```bash
# Full scan: S&P 500 → top 5 → 25 prompts × Claude + Mistral → watchlist:
python day_orchestrator.py

# Bypass screener, test specific tickers:
python day_orchestrator.py --tickers NVDA TSLA

# Print every AI prompt + response:
python day_orchestrator.py --tickers NVDA --verbose
```

Output: `watchlist/watchlist_YYYYMMDD.json` + Telegram summary.

### ORB Engine (Phase 2)

Run at **~15:25 NL / 09:25 ET** (just before the US open).

```bash
python orb_strategy.py
# or via Task Scheduler:
run_us.bat
```

Reads today's watchlist, waits for the 09:30 candle, watches 1-min bars for breakout + retest, places bracket orders, monitors until EOD flatten.

### Integration Test

End-to-end pipeline test: historical bars → AI → ORB → IBKR bracket order.

```bash
# Full test (all 9 steps):
python test_integration.py --ticker NVDA --date 2026-06-24

# Fast (skip AI prompts — tests data + ORB + order placement):
python test_integration.py --ticker NVDA --date 2026-06-24 --skip-ai

# Leave orders open to inspect in TWS:
python test_integration.py --ticker NVDA --date 2026-06-24 --skip-ai --no-cancel
```

**Test passes** when all 3 bracket legs (entry limit + stop loss + take profit) appear in IBKR open orders.

---

## Strategy Logic

### Opening Range Breakout (ORB) — per the 9:30 Candle Algorithm

```
1. At 09:30 ET:  record HIGH and LOW of the first 5-min candle
2. Switch to 1-min bars
3. Watch for BREAKOUT (close above high → LONG candidate,
                        close below low  → SHORT candidate)
4. Wait for RETEST:
     LONG  — bar low touches range high AND closes above it → ENTER LONG
     SHORT — bar high touches range low AND closes below it → ENTER SHORT
     Failed retest (closes wrong side) → skip today
5. Bracket order:
     Entry = breakout level  |  Stop = opposite range edge  |  TP = 2R
6. Exits: TP hit / SL hit / price re-enters range / 16:30 window close / EOD flatten
```

### Direction Filter (Signal Mesh integration)

| Mesh signal | ORB action |
|-------------|------------|
| `BUY`  | Long only — act only on an **upside** breakout |
| `SELL` | Short only — act only on a **downside** breakout |
| `HOLD` | Skip entirely |

### RVOL Gate (tiered)

| Premarket RVOL | Effect |
|----------------|--------|
| < 1.5× | Hard NOTHING — skip (not in play) |
| 1.5× – 3× | Tradeable, conviction capped at 60 |
| ≥ 3× | Full conviction |

---

## Capital & Risk

| Parameter | Value |
|-----------|-------|
| House money | €5,000 EUR |
| Risk per trade | 1% (≈ €50) |
| Max concurrent positions | 3 |
| Max trades per symbol/day | 1 |
| Trade currency | USD (converted at live EUR/USD) |
| TP | 2R (configurable via `tp_r_multiple`) |

All parameters live in `config.INTRADAY_PARAMS` — a single `SimpleNamespace` that both the live engine and the backtest read, so tuning one moves both.

---

## AI Prompt Architecture

25 prompts across 5 categories, run by Claude + Mistral in parallel per ticker:

| Category | Weight | Focus |
|----------|--------|-------|
| `price_action` | 30% | Gap type, key levels, premarket structure, open scenario |
| `quant_edge` | 25% | RVOL gate, gap stats, ATR room, risk sizing, setup grade |
| `catalyst` | 20% | Catalyst type, freshness, direction alignment, event risk |
| `sentiment_flow` | 15% | News tone, social velocity, options positioning |
| `market_regime` | 10% | Futures, sector, VIX, macro calendar |

Each prompt returns `LONG / SHORT / NOTHING` + conviction (0–100). A cross-pollination round lets each agent review the other's conclusions before final aggregation.

---

## Phase Status

| Phase | Description | Status |
|-------|-------------|--------|
| **Phase 0** | Foundation: strategy abstraction, ORB core, IBKR/Telegram adapters | ✅ Done |
| **Phase 1** | Premarket AI screener → watchlist | ✅ Done |
| **Phase 2** | Live IBKR ORB engine (threading, EOD flatten) | 🔧 Skeleton (sequential) |
| **Phase 3** | Backtest harness + metrics | ✅ Done |

### Remaining Phase 2 items

- [ ] Multi-ticker threading (`threading.Thread` per pick)
- [ ] Hard EOD flatten fallback for all open positions

---

## Telegram Notifications

| Event | When |
|-------|------|
| Shortlist selected | Phase 1 screener finishes gap scan |
| Per-ticker analysis | After AI mesh + aggregation per ticker |
| Orders placed | ORB confirms breakout + retest |
| Trade exits | TP / SL / range re-entry / EOD |
| Day summary | After EOD flatten |
