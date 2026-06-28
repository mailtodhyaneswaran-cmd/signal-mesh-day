"""
Copy this file to config.py and fill in your credentials before running.
config.py is excluded from git via .gitignore.
"""
from types import SimpleNamespace

# ── IBKR connection ───────────────────────────────────────────────────────────
IBKR_HOST      = "127.0.0.1"
LIVE_TRADING   = False
IBKR_PORT      = 7496 if LIVE_TRADING else 7497   # TWS: 7497 paper / 7496 live
IBKR_CLIENT_ID = 36                                # keep different from candle-scalping-bot (35)

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN"
TELEGRAM_CHAT_ID   = "YOUR_CHAT_ID"

# ── AI agents ─────────────────────────────────────────────────────────────────
# Claude uses the Claude Code CLI (no key needed — authenticated via CLI login).
# Mistral API key from https://console.mistral.ai/api-keys
MISTRAL_API_KEY = "YOUR_MISTRAL_API_KEY"

# ── Capital & risk ────────────────────────────────────────────────────────────
HOUSE_MONEY_EUR           = 5_000   # total deployable capital — never exceed principal
RISK_PER_TRADE_PCT        = 0.01    # 1 % per trade ≈ €50
MAX_CONCURRENT_POSITIONS  = 3       # one per screener pick
MAX_TRADES_PER_SYMBOL_PER_DAY = 1

# ── Files ─────────────────────────────────────────────────────────────────────
from setup_paths import WATCHLIST_DIR, STATE_FILE as _SP_STATE
WATCHLIST_DIR = str(WATCHLIST_DIR)
STATE_FILE    = str(_SP_STATE)

# ── Intraday params — single source of truth for live bot AND backtest ────────
# Mechanical knobs (rvol, gap, ATR) tune on the historical backtest.
# LLM-dependent knobs (category_weights, direction_threshold) validate forward
# on paper only — never backtest these (lookahead bias).
INTRADAY_PARAMS = SimpleNamespace(
    # ── ORB mechanics ────────────────────────────────────────────────
    orb_range_minutes     = 5,
    orb_rvol_gate         = 1.5,     # breakout candle RVOL floor (live gate)
    tp_r_multiple         = 2.0,     # take-profit = entry ± (R × stop-distance)
    sl_mode               = "range_edge",  # "range_edge" | "atr"
    atr_mult              = 1.0,
    require_retest        = True,
    retest_tolerance_pct  = 0.0005,  # 5 bps of mid-range price
    min_range_pct         = 0.0015,  # skip days where opening range < 0.15 % of price
    rvol_rolling_window   = 10,      # bars to look back for RVOL baseline
    poll_interval_sec     = 60,
    breakout_window_end   = "16:30",  # NL time: no new breakout entries after this

    # ── Screener gates (Phase 1) ──────────────────────────────────────
    rvol_lookback_days    = 20,      # days of premarket history for RVOL baseline (IBKR)
    rvol_hard_floor       = 1.5,     # < this → hard NOTHING veto
    rvol_full_conviction  = 3.0,     # >= this → full conviction (no cap)
    rvol_midtier_cap      = 60,      # conviction clamped here for 1.5–3× RVOL
    gap_min_pct           = 1.5,     # minimum |premarket gap| % to qualify
    min_dollar_volume     = 20_000_000,
    shortlist_size        = 5,
    max_per_sector        = 2,
    direction_threshold   = 0.15,    # net weighted vote to declare LONG/SHORT

    # ── Screener category weights (Phase 1 — tune forward on paper only) ──
    # Keys MUST match day_trading_prompts.ALL_INTRADAY_PROMPTS categories.
    category_weights = {
        "price_action":   0.30,
        "quant_edge":     0.25,
        "catalyst":       0.20,
        "sentiment_flow": 0.15,
        "market_regime":  0.10,
    },

    # ── IB strategy (60-min Initial Balance breakout) ────────────────
    ib_range_minutes      = 60,    # IB range = first 60 min of session

    # ── VWAP strategy (mean-reversion) ───────────────────────────────
    vwap_min_deviation    = 0.015, # 1.5 % from VWAP to trigger entry
    vwap_tp_r_multiple    = 1.5,   # TP = 1.5R (more conservative than ORB's 2R)
    vwap_require_reversal = True,  # wait for bar closing back toward VWAP
    vwap_warmup_bars      = 15,    # bars before looking for trades
    vwap_stop_atr_mult    = 1.0,   # stop distance = 1× bar ATR

    # ── Cost model ────────────────────────────────────────────────────
    slippage_pct          = 0.0002,
    commission_per_share  = 0.005,
)
