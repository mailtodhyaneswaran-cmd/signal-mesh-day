# Signal Mesh — Intraday ORB Day-Trading System — TODO

**Purpose:** Bolt an intraday day-trading layer onto Signal Mesh. The existing mesh (daily, swing-horizon) becomes a *screener / directional bias generator*. A separate IBKR engine executes a 5-minute Opening Range Breakout (ORB) on the screener's 3 picks, sets bracket TP/SL, and flattens everything by the US close.

**Reference repo for all IBKR work:** `github.com/mailtodhyaneswaran-cmd/candle-scalping-bot`
(already implements 5-min ORB, RVOL ≥ 1.5× gate, retest confirmation, bracket orders, 2R TP, range/window/EOD exits, `ib_async` connector, paper/live toggle, Telegram, Task Scheduler launchers). Task 2 is mostly *adaptation* of this, not greenfield.

> **Scope:** research / paper-trading only. Not financial advice. `LIVE_TRADING` stays `False` until validated.

---

## Capital & Risk (house money)

| Constant | Value | Notes |
|---|---|---|
| `HOUSE_MONEY_EUR` | **€5,000** | Total deployable capital. Principal is the ceiling — do not exceed. |
| `BASE_CURRENCY` | EUR | IBKR paper account base currency. |
| `TRADE_CURRENCY` | USD | US stocks trade in USD → FX conversion needed. |
| `USD_BUDGET` | €5,000 × spot EURUSD | Convert at run time (see below). |
| `RISK_PER_TRADE_PCT` | 1% (≈ €50) | Per-trade risk = `HOUSE_MONEY_EUR × pct`. Position size = risk ÷ stop-distance. |
| `MAX_CONCURRENT_POSITIONS` | 3 | One per screener pick. |
| `MAX_TRADES_PER_SYMBOL_PER_DAY` | 1 | Matches reference bot. |

- [ ] Pull spot **EUR→USD** at screener time via `yfinance` ticker `EURUSD=X` (free); store in watchlist JSON so live + logs agree on the rate used.
- [ ] Size all US positions against `USD_BUDGET`, never against EUR notional directly (avoids over-leveraging on FX drift).
- [ ] Confirm IBKR paper account base currency = EUR and that it auto-converts (or borrows USD) for US trades; note any FX commission in cost modelling.
- [ ] Hard guard: refuse any order whose notional would push total exposure past `USD_BUDGET`.

---

## Data Sources (FMP dropped — all free / open-source)

| Need | Source | How |
|---|---|---|
| S&P 500 universe | Wikipedia constituents / maintained CSV | scrape + cache weekly |
| Daily price / volume / RVOL inputs | **Yahoo (`yfinance`)** | `download(..., prepost=True)` for pre-market bars; daily volume vs trailing avg |
| FX rate | **Yahoo (`yfinance`)** | `EURUSD=X` |
| News / catalyst | **`yfinance.Ticker(t).news`** (free, no key) + **Finnhub free tier** (company news) + RSS fallback (Nasdaq / company feeds) + optional **IBKR `reqHistoricalNews`** (free providers only, e.g. Briefing general) | aggregate headlines per candidate |
| **ORB intraday bars (live + opening range)** | **IBKR** | `reqHistoricalData` (1-min) for opening range; `reqRealTimeBars` (5-sec) or polled 1-min for breakout watch |
| Backtest minute bars | **IBKR `reqHistoricalData`** | cache locally; mind IBKR historical pacing limits |

---

## Signal → Direction Mapping (applies everywhere)

| Mesh / screener signal | Action |
|---|---|
| **BUY** | `long` — act only on an **upside** 5-min breakout |
| **SELL** | `short` — act only on a **downside** 5-min breakout |
| **HOLD** | `skip` — ignore the ticker entirely, even if it breaks out |

A BUY stock that breaks *down*, or a SELL stock that breaks *up*, → **do nothing**.

---

## Phase 0 — Foundation

- [ ] Create new repo `signal-mesh-day` (keep separate from `signal-mesh`; horizons differ).
- [ ] Define the **handoff contract** `watchlist_YYYYMMDD.json` (schema in Appendix A) — written by Task 1, read by Task 2 and Task 3.
- [ ] **Pluggable strategy architecture** (so new strategies are plug-and-play later):
  - [ ] `strategy_base.py` — abstract interface every strategy implements: `evaluate(bars, bias, params) -> StrategySignal{direction, entry, stop, target}`, plus a `name` and its own declared param keys. The live bot and backtester both call strategies *only* through this interface.
  - [ ] `orb_strategy.py` — the 5-min ORB as the **first** concrete implementation (opening-range capture, breakout test, retest). Lives behind `strategy_base`.
  - [ ] `orb_core.py` — keep as the shared low-level primitives ORB uses (range capture, breakout/retest helpers), importable by future strategies too.
  - [ ] Keep system-wide mechanics OUT of the strategy file — position sizing, bracket builder, EOD flatten, live RVOL gate, IBKR order plumbing belong to the engine. Split is: strategy decides **signal + levels**; engine handles **execution + risk**.
  - [ ] Select strategy by config: `STRATEGY = "orb"` (same plug-and-play pattern as the planned `lib_broker.py` Alpaca/IBKR switch). Adding a strategy = drop a new file + register it, no rewrite.
  - [ ] Design note: build ORB cleanly behind the interface now, but let **strategy #2** (e.g. VWAP reclaim, mean-reversion) reshape the interface — don't over-abstract against a single implementation. Payoff lands on #2.
  - [ ] Bonus: because live + backtest share `strategy_base`, the Phase 3 harness can backtest **any** future strategy with zero new harness code — just register it.
- [ ] Copy + adapt from candle-scalping-bot: `ibkr_connector.py`, `telegram_notify.py`, `config.example.py`, `test_connection.py`.
- [ ] `config.py`: capital/risk constants above, IBKR port (7497 TWS / 4001 Gateway paper), `IBKR_CLIENT_ID`, `LIVE_TRADING=False`, Telegram creds.
- [ ] **Currency correctness (carried over from `signal-mesh` bug):** never *assume* an instrument's currency from a flag — always read it from the data source. In the existing orchestrator the `€`/`$` symbol is driven only by `--euro`, so a USD-listed name (e.g. NASDAQ `ASML`) gets mislabelled `€`. The signal math is unit-free and unaffected, but **absolute prices** (`price`, `target_price`, ATR stops, entry) are mislabelled — which becomes a real **position-sizing error** here, since we convert €5,000 → USD and size off price.
  - [ ] Read true currency per ticker: `ccy = info.get("currency", "USD")` (yfinance) / from the IBKR contract; store it on the data dict and in `watchlist.json` per pick.
  - [ ] Label and format all displays/Telegram from `data["currency"]`, not from any mode flag.
  - [ ] Guard: if the instrument currency ≠ `TRADE_CURRENCY` (USD), either reject the pick or convert via the live `EURUSD=X` rate (same rate used for sizing) so the analysed listing and the sizing currency always agree.
  - [ ] Apply the same fix back in `signal_mesh_orchestrator.py` (`fetch_stock_data` + the two display sites at ~L1224/L1335) so both systems share the corrected behaviour.
- [ ] **Real news headlines (fix empty-`[]` stub; shared data-layer):** today's logs confirm the sentiment prompt is fed `news_headlines = []`, so the AI votes on no news at all. Wire a real per-ticker feed into `fetch_stock_data` (alongside the price pull) so both the swing system and the day-trading screener inherit it.
  - [ ] Primary source — **`yfinance.Ticker(t).news`** (free, no key): map each item to `{title, publisher, date}`, keep the most recent ~8.
  ```python
  import yfinance as yf
  items = yf.Ticker(ticker).news or []
  headlines = [
      {"title": i["content"]["title"],
       "publisher": i["content"]["provider"]["displayName"],
       "date": i["content"]["pubDate"]}
      for i in items[:8]
  ]
  ```
  - [ ] Replace the `json.dumps([])` stub (~L721) and the `"No recent news"` stub (~L719) with this real list; also drop `has_stub_data` once sentiment is grounded.
  - [ ] Filter to last ~48h, dedupe by title, cap at N most recent so the prompt isn't flooded.
  - [ ] Interim until wired: fall back to STEP1's per-pick `reason` + `key_sources` (already produced, currently discarded) so sentiment is never voting on empty input.
  - [ ] Supplement/fallback feeds: Finnhub free-tier `company-news` (date-bounded) and per-ticker RSS (Yahoo/Nasdaq/Google News). All free.

---

## Phase 1 — Task 1: S&P 500 AI Screener → 3 stocks + direction

## Phase 1 — Task 1: S&P 500 AI Screener → premarket bias on the day's stocks

> Runs ~15:00 NL (~09:00 ET) = **premarket**. Output is a directional **bias + levels**, not a trigger. ORB confirms live at the open (the two-confirmation design). The mesh prompts and aggregation live in `day_trading_prompts.py`.

**Add the day-trading prompts into the prompts module**
- [ ] Add the 25 day-trading prompts (the `day_trading_prompts.py` set) into **`prompts.py`** — keep them separate from the swing set (`analysis_prompts.py`), e.g. import/expose them as the intraday registry so the day orchestrator pulls these, not the swing 25.
- [ ] Outputs are **LONG / SHORT / NOTHING** (+ `conviction`, `risk_level`), never BUY/SELL/HOLD.
- [ ] Keep `INTRADAY_PARAMS` as the single source of truth for every threshold/weight (so the backtest tunes in one place).

**Screener funnel (500 → 5)**
- [ ] Stage 0 — cached S&P 500 constituents (weekly refresh).
- [ ] Stage 1 — coarse premarket scan via **IBKR `reqScannerData`** (`TOP_PERC_GAIN`, `TOP_PERC_LOSE`, `MOST_ACTIVE`/`HOT_BY_VOLUME`); pull gainers *and* losers, intersect with the cached universe → ~30–60 active names. (Avoids 500 one-by-one premarket fetches / rate limits.)
- [ ] Stage 2 — enrich only survivors (Yahoo `prepost=True` / IBKR) and compute the volume score:
  `in_play_score = screener_weights.rvol·rvol_rank + .gap·gap_rank + .catalyst·catalyst_score` (weights from `INTRADAY_PARAMS`).
- [ ] Stage 3 — hard gates (drop, don't rank): RVOL ≥ `rvol_hard_floor`, |gap| ≥ `gap_min_pct`, dollar-volume ≥ `min_dollar_volume`, real catalyst for the top tier.
- [ ] Stage 4 — sort by score, dedupe by sector (`max_per_sector`), take top `shortlist_size` (=5). Provisional direction = sign of gap (mesh confirms/overrides).
- [ ] **RVOL baseline caching:** maintain a rolling premarket-volume-by-time cache per candidate; until warm, fall back to `today_premkt_vol ÷ (avg_daily_volume × typical_premarket_fraction)`. Verify IBKR premarket market-data permissions on the paper account early.

**Premarket data fetch (per shortlisted name)**
- [ ] Populate the full premarket DATA CONTRACT in `day_trading_prompts.py` (gap, premarket H/L, RVOL, ATR, float, short %, prior levels, ES/NQ + SPY/QQQ premarket, VIX, sector, news headlines, macro events, USD capital + risk budget). Reuse the shared real-news feed from Phase 0.

**Mesh + aggregation**
- [ ] Run the 25 day prompts across the 2 agents (Claude + Mistral) on each of the 5, then **cross-pollination** (conviction-spread aware for the 2-agent case).
- [ ] **Tiered RVOL gate**, enforced deterministically via `rvol_conviction_cap(rvol, params)`: `<1.5×` → hard NOTHING (veto, skip scoring); `1.5–3×` → conviction clamped to `rvol_midtier_cap` (60); `≥3×` → full.
- [ ] Aggregate with `aggregate_ticker(prompt_results, rvol, params)`: vote LONG=+1/SHORT=−1/NOTHING=0, weight by `category_weights` and capped conviction, compare net to `direction_threshold`.
- [ ] Carry per-name `currency` (Phase 0 fix) and the EURUSD rate into the output.
- [ ] Write `watchlist_YYYYMMDD.json` (Appendix A): direction, conviction, entry_zone, invalidation, target, suggested_qty per pick.
- [ ] Telegram summary of the picks (ticker, direction, RVOL tier, catalyst, conviction).
- [ ] Schedule via Task Scheduler (~15:00 NL / ~09:00 ET, before the open).

> **Tuning split:** mechanical knobs (`rvol_*`, `gap_min_pct`, ATR/`min_reward_risk`) tune on the historical ORB backtest; the LLM-dependent knobs (`category_weights`, `direction_threshold`) validate **forward on paper** only (lookahead bias — see Phase 3).

---

## Phase 2 — Task 2: IBKR 5-min ORB Execution

- [ ] Connect to IBKR paper via adapted `ibkr_connector.py`; verify with `test_connection.py` (bracket order round-trip).
- [ ] Load `watchlist_YYYYMMDD.json`; keep only `long`/`short` tickers.
- [ ] At US open per ticker: fetch first 5-min candle from IBKR → record opening-range high/low (via `orb_core`).
- [ ] Poll 1-min candles (IBKR `reqRealTimeBars`/historical); detect close outside range.
- [ ] Apply the **live RVOL gate** on the breakout candle using `INTRADAY_PARAMS["orb_rvol_gate"]` (not a hardcoded 1.5) — a binary mechanical pass/fail on real intraday volume (mirrors the reference bot + the GLW volume-gate lesson). This is distinct from Phase 1's *tiered* gate: ORB has no conviction to scale, so it's one floor, but it's tuned from the same params block so the backtest moves both together.
- [ ] **Direction gating** per the mapping table (long-only on BUY, short-only on SELL).
- [ ] **Retest confirmation** before entry (reuse reference logic).
- [ ] Position sizing: `risk = HOUSE_MONEY_EUR × RISK_PER_TRADE_PCT`, converted to USD; `qty = risk_usd ÷ stop_distance`; clamp to `USD_BUDGET` / `MAX_CONCURRENT_POSITIONS`.
- [ ] Place **bracket order**: entry + SL (ATR multiple or opposite range edge) + TP (start 2R, tune from Task 3).
- [ ] Exits: TP, SL, price re-enters range, session-window close, **+ hard EOD flatten** (nothing held overnight).
- [ ] Per-day `state.json`, session log, Telegram events (reuse reference message set).
- [ ] `run_us.bat` + Task Scheduler at ~15:25 NL.
- [ ] Paper-trade end-to-end for several weeks before considering `LIVE_TRADING=True`.

---

## Telegram Notifications — full day timeline

> Reuse `telegram_notify.py` (from candle-scalping-bot) and the message style of the current swing Signal Mesh (HTML parse mode, emoji status, ranked tables). Two processes write to the same chat: the **Phase 1 orchestrator** (screener + mesh, ~14:55 NL) and the **Phase 2 ORB bot** (open onward). Keep a shared `chat_id`; tag each message with which stage sent it.

- [ ] **① ~14:55 NL — Picks selected for analysis.** When the screener finishes the 500→5 funnel, send the shortlist: per ticker — gap %, RVOL (and tier), provisional direction, catalyst, sector. (Mirrors the current Signal Mesh "discovered stocks" notification.)
- [ ] **② Per-ticker analysis result.** After the 25-prompt mesh + cross-pollination on each name (before the open), send the analysis like the current Signal Mesh ranked output: final **LONG / SHORT / NOTHING**, conviction, RVOL tier, key levels (entry zone / invalidation / target), suggested qty, and the top 1–2 reasons. One message per ticker or one ranked batch — match the swing format.
- [ ] **③ ~15:30 NL open / post-ORB — orders placed.** When ORB confirms the bias and a bracket is submitted, notify: direction, entry/limit price, SL, TP, qty, USD exposure (e.g. "✅ Long NVDA entry 168.40, SL 167.10, TP 171.00, qty 30, exposure $5,052"). Also send a "😴 no clean ORB setup / stood aside" message for picks that didn't trigger, so silence is never ambiguous.
- [ ] **④ Trade actions / exits.** Notify on every fill or exit event: 🎯 TP hit (+P&L), ❌ SL hit (−P&L), 🚪 price re-entered range → exited, ⏰ EOD flatten. Reuses the reference bot's event set.
- [ ] **⑤ End-of-day summary.** After the EOD flatten, send a wrap-up: each pick's outcome (traded long/short/skipped), realized P&L per trade and total, win/loss count, and remaining house-money balance. (Analogous to the swing system's run-end summary + the weekly P&L idea in the spec.)
- [ ] Robustness: keep the stdlib `urllib` sender + 429 retry/backoff from the swing system; if Telegram is unreachable, log locally and continue (never block a trade on a failed notification).

---

## Phase 3 — Task 3: Backtest

- [ ] Build IBKR historical minute-bar loader with local cache (respect pacing limits).
- [ ] Import the **same `orb_core.py`** as live — identical tested logic.
- [ ] **Stub the screener** via pluggable `select_universe(date)`: for backtest, "always pick a certain S&P stock" (fixed ticker, or deterministic top-N-by-RVOL) to remove the screener variable and isolate ORB mechanics.
- [ ] **Direction = mechanical, not simulated AI.** ⚠️ You can't faithfully replay Claude historically — the model already knows the outcome, so simulated AI direction = lookahead bias = inflated results. Use pure ORB direction (long if 5-min candle green, short if red) or fixed long-only. Validate the AI layer *forward* via paper trading only.
- [ ] Apply same gates as live, all read from `INTRADAY_PARAMS` (so a sweep changes backtest + live together): `orb_rvol_gate`, retest, bracket TP/SL (`tp_r_multiple`), EOD flatten.
- [ ] Cost model: commission + slippage + spread + FX conversion cost.
- [ ] Apply €5,000 capital + 1% risk sizing so equity curve reflects real constraints.
- [ ] Metrics: win rate, avg R, total/annualized return, Sharpe, max drawdown, profit factor, trades/day; equity-curve plot; trades CSV.
- [ ] Walk-forward / out-of-sample split.
- [ ] Sanity-check against a second data source where possible (same code can give materially different results per provider).
- [ ] Feed tuned params (opening-range duration, RVOL threshold, TP/SL multiples) back into Task 2 config.

---

## Recommended Build Order

1. Phase 0 foundation (repo, schema, `orb_core`, IBKR connector copy).
2. Phase 3 backtest on `orb_core` with a fixed ticker — **prove the mechanical edge first.**
3. Phase 2 live paper wiring (reusing the now-validated `orb_core`).
4. Phase 1 screener last — plug Claude's 3 picks into the proven pipeline.
5. Paper-trade the full loop for months before any real capital.

---

## Appendix A — `watchlist_YYYYMMDD.json` schema

```json
{
  "date": "2026-06-22",
  "generated_at_utc": "2026-06-22T07:35:00Z",
  "eur_usd_rate": 1.08,
  "house_money_eur": 5000,
  "usd_budget": 5400,
  "picks": [
    {
      "ticker": "NVDA",
      "signal": "BUY",
      "direction": "long",
      "currency": "USD",
      "rvol": 2.3,
      "catalyst": "analyst upgrade pre-market",
      "confidence": 0.74,
      "reasoning": "…",
      "avg_daily_volume": 41000000,
      "last_price": 168.40
    }
  ]
}
```
- `direction` ∈ `long` | `short` | `skip`. Consumers drop `skip`.

## Appendix B — Strategy interface + `orb_core.py` primitives (shared live + backtest)

```python
# --- strategy_base.py : the plug-and-play contract -----------------------
@dataclass
class StrategySignal:
    direction: str          # "long" | "short" | "skip"
    entry: float
    stop: float
    target: float
    qty: int = 0            # filled by the engine's sizing, not the strategy

class Strategy(Protocol):
    name: str
    def evaluate(self, bars, bias, params) -> StrategySignal: ...
    # bias = the premarket LONG/SHORT/NOTHING from watchlist.json
    # bars = intraday bars (IBKR live, or cached historical in backtest)

# orb_strategy.py implements Strategy; future files (vwap_strategy.py,
# meanrev_strategy.py) implement the same Protocol. Engine picks by config:
#   STRATEGY = "orb"  ->  registry["orb"]() 

# --- orb_core.py : shared low-level primitives ORB (and others) reuse ----
@dataclass
class ORBConfig:
    range_minutes: int = 5
    rvol_min: float = 1.5
    tp_r_multiple: float = 2.0
    sl_mode: str = "atr"        # "atr" | "range_edge"
    atr_mult: float = 1.0
    require_retest: bool = True
    risk_per_trade_eur: float = 50.0

@dataclass
class OpeningRange:
    ticker: str
    high: float
    low: float
    rvol: float

def capture_opening_range(bars, cfg) -> OpeningRange: ...
def detect_breakout(range_, candle, direction) -> bool:
    # honors direction gating: long→up only, short→down only
    ...
def confirm_retest(range_, candles, cfg) -> bool: ...
def build_bracket(range_, entry_price, direction, cfg, usd_budget) -> dict:
    # returns {entry, stop, take_profit, qty}
    ...
```
Live engine and backtest both call strategies *only* through `Strategy.evaluate()`, so they always run identical logic — and any future strategy works in both with zero new harness code. `orb_strategy.py` decides signal + levels; the engine owns sizing, bracket, EOD flatten, and the live RVOL gate.

## Appendix C — Free news source notes

- `yfinance.Ticker(t).news` — zero-key, fastest to wire.
- **Finnhub** free tier — company news endpoint, generous limits, needs free key.
- RSS fallback — Nasdaq / company investor feeds for catalysts.
- IBKR `reqNewsProviders` / `reqHistoricalNews` — use only *free* providers; many IBKR feeds require paid subscriptions.
