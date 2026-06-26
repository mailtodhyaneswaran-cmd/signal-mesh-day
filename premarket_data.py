"""
premarket_data.py — Premarket data layer for the day-trading screener.

All yfinance calls are wrapped in try/except; missing fields fall back to
sensible stubs so the 25 prompts can still run (with lowered conviction per spec).

Public API
──────────
  fetch_market_context()                              -> dict
  batch_gap_scan(tickers, params)                     -> list[dict]
  enrich_ticker(ticker, mkt_ctx, candidates, eurusd, params) -> dict
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

import config
import ibkr_connector

# Sector → ETF mapping for sector_premarket_pct prompt field
SECTOR_ETF_MAP = {
    "Technology":             "XLK",
    "Health Care":            "XLV",
    "Financials":             "XLF",
    "Consumer Discretionary": "XLY",
    "Communication Services": "XLC",
    "Industrials":            "XLI",
    "Consumer Staples":       "XLP",
    "Energy":                 "XLE",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
    "Materials":              "XLB",
}

# Premarket volume as fraction of avg daily volume (used when 30d premarket
# history is unavailable — matches todo.md RVOL baseline fallback note).
_PM_VOL_FRACTION = 0.05


# ── Market context ────────────────────────────────────────────────────────────

def fetch_market_context() -> dict:
    """
    Fetch broad-market premarket context.

    SPY / QQQ: uses fast_info (last_price vs prior close) so it works premarket.
    ES=F / NQ=F: daily close-to-close from yfinance download.
    ^VIX: daily close-to-close.

    Returns a flat dict of string values ready for prompt .format_map().
    """
    ctx = {
        "spy_premarket_pct": "n/a",
        "qqq_premarket_pct": "n/a",
        "es_futures_pct":    "n/a",
        "nq_futures_pct":    "n/a",
        "vix":               "n/a",
        "vix_change":        "n/a",
    }

    # SPY and QQQ via fast_info — works premarket (last_price = current price)
    for sym, key in [("SPY", "spy_premarket_pct"), ("QQQ", "qqq_premarket_pct")]:
        try:
            fi         = yf.Ticker(sym).fast_info
            last       = getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
            prev_close = getattr(fi, "regular_market_previous_close", None)
            if last and prev_close and float(prev_close) > 0:
                pct = (float(last) - float(prev_close)) / float(prev_close) * 100
                ctx[key] = f"{pct:+.2f}%"
        except Exception:
            pass

    # ES/NQ futures and VIX via batch daily download
    try:
        raw = yf.download(
            ["ES=F", "NQ=F", "^VIX"], period="3d", interval="1d",
            auto_adjust=True, progress=False, threads=True,
        )
        if not raw.empty:
            try:
                closes = raw["Close"]
            except KeyError:
                closes = raw.xs("Close", axis=1, level=0)

            def _pct(sym: str) -> str:
                try:
                    s = closes[sym].dropna()
                    return f"{(s.iloc[-1] / s.iloc[-2] - 1) * 100:+.2f}%" if len(s) >= 2 else "n/a"
                except Exception:
                    return "n/a"

            ctx["es_futures_pct"] = _pct("ES=F")
            ctx["nq_futures_pct"] = _pct("NQ=F")
            try:
                vix_s = closes["^VIX"].dropna()
                if len(vix_s) >= 2:
                    ctx["vix"]        = f"{vix_s.iloc[-1]:.2f}"
                    ctx["vix_change"] = f"{(vix_s.iloc[-1] / vix_s.iloc[-2] - 1)*100:+.2f}%"
            except Exception:
                pass
    except Exception as e:
        print(f"[premarket] futures/VIX context error: {e}")

    return ctx


# ── Batch coarse scan ─────────────────────────────────────────────────────────

def batch_gap_scan(tickers: list[str], params) -> list[dict]:
    """
    Stage 1+2: find the day's biggest premarket movers from the S&P 500.

    Steps
    ─────
    1. Batch-download 5d of daily closes + volumes for all 500 in one call.
    2. Sort by |1-day return| (close-to-close proxy for overnight gap).
    3. For the top 60, fetch real premarket price via fast_info (serial, ~60s).
    4. Apply hard gates: |gap| ≥ gap_min_pct, dollar-vol ≥ min_dollar_volume.
    5. Sort by score and return up to shortlist_size × 3 (buffer for sector dedup).
    """
    print(f"[screener] Batch downloading {len(tickers)} tickers (daily, 5d)...")
    try:
        raw = yf.download(
            tickers, period="5d", interval="1d",
            auto_adjust=True, progress=False, threads=True,
        )
    except Exception as e:
        print(f"[screener] Batch download failed: {e}")
        return []

    if raw.empty:
        print("[screener] No data returned from yfinance.")
        return []

    try:
        closes  = raw["Close"]
        volumes = raw["Volume"]
    except KeyError:
        print("[screener] Unexpected yfinance column structure.")
        return []

    # Ensure DataFrame (not Series) even if a single ticker slipped through
    if isinstance(closes, pd.Series):
        closes  = closes.to_frame(name=tickers[0])
        volumes = volumes.to_frame(name=tickers[0])

    first_pass = []
    for ticker in closes.columns:
        try:
            c = closes[ticker].dropna()
            v = volumes[ticker].dropna()
            if len(c) < 2:
                continue
            prior = float(c.iloc[-2])
            last  = float(c.iloc[-1])
            if prior <= 0:
                continue
            avg_vol = float(v.tail(10).mean()) if len(v) >= 10 else float(v.mean())
            first_pass.append({
                "ticker":      str(ticker),
                "prior_close": round(prior, 4),
                "approx_gap":  round((last - prior) / prior * 100, 2),
                "avg_volume":  avg_vol,
            })
        except Exception:
            pass

    # Sort by absolute gap; enrich top 60 with real premarket prices
    first_pass.sort(key=lambda x: abs(x["approx_gap"]), reverse=True)
    top60 = first_pass[:60]
    print(f"[screener] Enriching top {len(top60)} candidates with premarket prices...")

    enriched = []
    for c in top60:
        ticker = c["ticker"]
        try:
            fi       = yf.Ticker(ticker).fast_info
            pm_price = float(fi.last_price or c["prior_close"])
            avg_vol  = float(getattr(fi, "three_month_average_volume", None) or c["avg_volume"])
            real_gap = (pm_price - c["prior_close"]) / c["prior_close"] * 100

            if abs(real_gap) < params.gap_min_pct:
                continue
            if pm_price * avg_vol < params.min_dollar_volume:
                continue

            enriched.append({
                "ticker":          ticker,
                "prior_close":     c["prior_close"],
                "premarket_price": round(pm_price, 4),
                "gap_pct":         round(real_gap, 2),
                "avg_volume":      round(avg_vol),
                "score":           abs(real_gap),
            })
        except Exception:
            pass

    enriched.sort(key=lambda x: x["score"], reverse=True)
    kept = enriched[: params.shortlist_size * 3]
    print(f"[screener] {len(enriched)} passed gates → keeping top {len(kept)}.")
    return kept


# ── Per-ticker full enrichment ────────────────────────────────────────────────

def enrich_ticker(
    ticker:     str,
    mkt_ctx:    dict,
    candidates: list[dict],
    eurusd:     float,
    params,
    ib=None,
) -> dict:
    """
    Full data enrichment for one shortlisted ticker.
    Populates every field required by the 25 day-trading prompts.

    ib: optional live IBKR connection. When provided, real premarket RVOL
        is computed from IBKR 1-min bars (useRTH=False) instead of the
        yfinance fallback estimate.
    """
    t = yf.Ticker(ticker)

    # ── 30-day daily history → ATR, avg volume, prior levels ─────────────
    prior_close = prior_high = prior_low = 0.0
    atr14 = avg_daily_volume = 0.0
    try:
        hist = t.history(period="30d", interval="1d")
        if not hist.empty:
            prior_close      = round(float(hist["Close"].iloc[-1]), 4)
            prior_high       = round(float(hist["High"].iloc[-1]),  4)
            prior_low        = round(float(hist["Low"].iloc[-1]),   4)
            avg_daily_volume = int(hist["Volume"].tail(20).mean())
            # 14-day ATR
            hi = hist["High"].tail(15)
            lo = hist["Low"].tail(15)
            cl = hist["Close"].tail(15)
            tr = pd.concat([
                hi - lo,
                (hi - cl.shift(1)).abs(),
                (lo - cl.shift(1)).abs(),
            ], axis=1).max(axis=1)
            atr14 = round(float(tr.tail(14).mean()), 4)
    except Exception as e:
        print(f"  [{ticker}] daily history error: {e}")

    atr_pct = round(atr14 / prior_close * 100, 3) if prior_close else 0.0

    # ── Today's premarket 1-min bars ──────────────────────────────────────
    pm_price = pm_high = pm_low = prior_close
    pm_volume = 0
    try:
        pm_bars = t.history(period="1d", interval="1m", prepost=True)
        if not pm_bars.empty:
            # Ensure tz-aware index before converting — yfinance may return naive timestamps
            idx = pm_bars.index
            if idx.tz is None:
                idx = idx.tz_localize("UTC")
            idx_et = idx.tz_convert("America/New_York")
            import datetime as _dt
            cutoff = _dt.time(9, 30)
            pm_mask = [t_val.time() < cutoff for t_val in idx_et]
            pm_only = pm_bars[pm_mask]
            if not pm_only.empty:
                pm_price  = round(float(pm_only["Close"].iloc[-1]), 4)
                pm_high   = round(float(pm_only["High"].max()), 4)
                pm_low    = round(float(pm_only["Low"].min()), 4)
                pm_volume = int(pm_only["Volume"].sum())
    except Exception as e:
        print(f"  [{ticker}] premarket 1-min bars error: {e}")

    # Fallback 1: fast_info current price + intraday volume (includes premarket)
    fi = t.fast_info
    if pm_price == prior_close:
        try:
            last = getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
            if last and float(last) > 0:
                pm_price = round(float(last), 4)
                pm_high  = pm_price
                pm_low   = pm_price
        except Exception:
            pass

    if pm_volume == 0:
        try:
            intraday_vol = getattr(fi, "regular_market_volume", None)
            if intraday_vol and int(intraday_vol) > 0:
                pm_volume = int(intraday_vol)
                print(f"  [{ticker}] premarket 1-min unavailable — using fast_info volume ({pm_volume:,})")
        except Exception:
            pass

    gap_pct             = round((pm_price - prior_close) / prior_close * 100, 2) if prior_close else 0.0
    avg_pm_vol_estimate = avg_daily_volume * _PM_VOL_FRACTION   # yfinance fallback only

    # ── Real RVOL via IBKR premarket 1-min bars ───────────────────────────
    # Formula: today's premarket vol (04:00–09:30 ET, useRTH=False)
    #          ÷ avg premarket vol same window over last 20 days
    # This is apples-to-apples. The × 0.05 estimate is only used as a last resort.
    rvol_premarket = 0.0
    if ib is not None:
        try:
            contract_ibkr     = ibkr_connector.get_contract(ticker)
            real_pm_volume    = ibkr_connector.get_premarket_volume_ibkr(ib, contract_ibkr)
            avg_pm_volume_20d = ibkr_connector.get_avg_premarket_volume_ibkr(
                ib, contract_ibkr,
                lookback_days=getattr(params, "rvol_lookback_days", 20),
            )
            if real_pm_volume > 0 and avg_pm_volume_20d > 0:
                rvol_premarket = round(real_pm_volume / avg_pm_volume_20d, 2)
                pm_volume      = real_pm_volume
                print(f"  [{ticker}] Real RVOL {rvol_premarket}x  "
                      f"(today {real_pm_volume:,}  avg20d {avg_pm_volume_20d:,.0f})")
            else:
                print(f"  [{ticker}] IBKR premarket bars returned no volume — using fallback")
        except Exception as e:
            print(f"  [{ticker}] IBKR RVOL error: {e}")

    # Fallback chain: yfinance intraday vol ÷ 5% estimate → hard floor
    if rvol_premarket == 0.0:
        if pm_volume > 0 and avg_pm_vol_estimate > 0:
            rvol_premarket = round(pm_volume / avg_pm_vol_estimate, 2)
            print(f"  [{ticker}] Estimated RVOL {rvol_premarket}x (yfinance / ×0.05 fallback)")
        else:
            rvol_premarket = float(getattr(params, "rvol_hard_floor", 1.5))
            print(f"  [{ticker}] Premarket volume unavailable — RVOL floor {rvol_premarket}x")

    # ── Ticker info: float, sector, earnings ─────────────────────────────
    float_shares = short_pct = 0.0
    sector = currency = "USD"
    sector = "Unknown"
    next_earnings = "N/A"
    try:
        info          = t.info
        float_shares  = float(info.get("floatShares") or 0)
        short_pct     = round(float(info.get("shortPercentOfFloat") or 0) * 100, 2)
        sector        = info.get("sector", "Unknown") or "Unknown"
        currency      = info.get("currency", "USD") or "USD"
        et = info.get("earningsTimestamp") or info.get("earningsDate")
        if et:
            try:
                next_earnings = datetime.fromtimestamp(int(et)).strftime("%Y-%m-%d")
            except Exception:
                next_earnings = str(et)
    except Exception as e:
        print(f"  [{ticker}] info fetch error: {e}")

    # ── Sector ETF premarket % ────────────────────────────────────────────
    sector_etf     = SECTOR_ETF_MAP.get(sector, "SPY")
    sector_pct_str = "n/a"
    try:
        etf = yf.download(sector_etf, period="2d", interval="1d",
                          prepost=True, progress=False, auto_adjust=True)
        if len(etf) >= 2:
            sector_pct_str = f"{(etf['Close'].iloc[-1] / etf['Close'].iloc[-2] - 1)*100:+.2f}%"
    except Exception:
        pass

    # ── Peer gaps from shortlist ──────────────────────────────────────────
    peer_gaps = ", ".join(
        f"{c['ticker']}: {c['gap_pct']:+.1f}%"
        for c in candidates if c["ticker"] != ticker
    ) or "no peers"

    # ── News + catalyst ───────────────────────────────────────────────────
    headlines, catalyst_summary = _fetch_news(ticker)

    # ── Capital / risk in USD ─────────────────────────────────────────────
    usd_capital        = round(config.HOUSE_MONEY_EUR * eurusd, 2)
    risk_per_trade_usd = round(usd_capital * config.RISK_PER_TRADE_PCT, 2)

    return {
        # Identity
        "ticker":                  ticker,
        "currency":                currency,
        "sector":                  sector,
        # Prior-day levels
        "prior_close":             f"{prior_close:.4f}",
        "prior_high":              f"{prior_high:.4f}",
        "prior_low":               f"{prior_low:.4f}",
        # Premarket
        "premarket_price":         f"{pm_price:.4f}",
        "premarket_high":          f"{pm_high:.4f}",
        "premarket_low":           f"{pm_low:.4f}",
        "premarket_gap_pct":       f"{gap_pct:.2f}",
        "premarket_volume":        str(pm_volume),
        "avg_premarket_volume_30d": str(int(avg_pm_vol_estimate)),
        "rvol_premarket":          f"{rvol_premarket:.2f}",
        # Volume / ATR / float
        "avg_daily_volume":        str(avg_daily_volume),
        "atr14":                   f"{atr14:.4f}",
        "atr_pct":                 f"{atr_pct:.3f}",
        "float_shares":            _fmt_large(float_shares),
        "short_pct_float":         f"{short_pct:.1f}",
        # Levels
        "round_levels":            _round_levels(pm_price),
        # News / catalyst (stubs for no-free-source fields per spec)
        "news_headlines":          json.dumps(headlines, indent=2),
        "social_metrics":          "no_data",
        "options_premarket":       "no_data",
        "catalyst_summary":        catalyst_summary,
        "next_earnings_date":      next_earnings,
        "macro_events_today":      "Check Forex Factory / investing.com for today's schedule",
        # Market context (injected from fetch_market_context)
        **mkt_ctx,
        # Sector
        "sector_etf":              sector_etf,
        "sector_premarket_pct":    sector_pct_str,
        "peer_gaps":               peer_gaps,
        # Capital
        "usd_capital":             f"{usd_capital:.2f}",
        "risk_per_trade_usd":      f"{risk_per_trade_usd:.2f}",
        # RVOL thresholds for QE1 prompt
        "rvol_hard_floor":         str(params.rvol_hard_floor),
        "rvol_full_conviction":    str(params.rvol_full_conviction),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fetch_news(ticker: str) -> tuple[list[dict], str]:
    """
    Fetch news headlines from yfinance. Handles both old and new SDK formats.
    Returns (headlines_list, catalyst_summary).
    """
    try:
        items     = yf.Ticker(ticker).news or []
        headlines = []
        for item in items[:8]:
            try:
                # New yfinance format (≥0.2): item["content"]["title"]
                content = item.get("content", {})
                title   = content.get("title") or item.get("title", "")
                pub     = (content.get("provider", {}).get("displayName")
                           or item.get("publisher", ""))
                dt      = content.get("pubDate") or str(item.get("providerPublishTime", ""))
                if title:
                    headlines.append({"title": title, "publisher": pub, "date": dt})
            except Exception:
                pass
        catalyst = headlines[0]["title"] if headlines else "No significant premarket news found"
        return headlines, catalyst
    except Exception:
        return [], "No news data available"


def _round_levels(price: float) -> str:
    """Return the nearest round-number price levels within ±15% of price."""
    levels = set()
    for step in (1, 5, 10, 25, 50, 100):
        lower = (price // step) * step
        upper = lower + step
        for lvl in (lower, upper):
            if lvl > 0 and abs(lvl - price) / price < 0.15:
                levels.add(round(lvl, 2))
    return ", ".join(f"${l:.2f}" for l in sorted(levels))


def _fmt_large(n: float) -> str:
    """Format large numbers as '12.3M', '450K', etc."""
    if n >= 1e9:  return f"{n/1e9:.1f}B"
    if n >= 1e6:  return f"{n/1e6:.1f}M"
    if n >= 1e3:  return f"{n/1e3:.0f}K"
    return str(int(n))
