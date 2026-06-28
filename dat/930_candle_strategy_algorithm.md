# 9:30 AM Candle Strategy — Full Algorithm

## Overview

The 9:30 AM Candle Strategy (also known as Opening Range Breakout / ORB) trades the breakout and retest of the first 5-minute candle after market open. The strategy runs two daily sessions:

| Session | Time (CEST) | Instrument |
|---------|-------------|------------|
| European | 9:00 AM – 10:30 AM | VUSA (Euronext Amsterdam) |
| US | 3:30 PM – 5:00 PM | PLTR (NASDAQ) |

**Key Parameters:**
- Risk/Reward: 2R (target = 2× stop distance)
- Max risk per trade: 2% of account balance
- Max trades per session: 1
- Max trades per day: 2 (1 EU + 1 US)

---

## PHASE 1: Initialisation

```python
START at 3:25 PM CEST (5 mins before open)

SET variables:
  candle_high        = 0
  candle_low         = 0
  breakout_direction = None       # LONG or SHORT
  breakout_confirmed = False
  retest_confirmed   = False
  trade_taken        = False
  window_open        = True
```

---

## PHASE 2: Capture the 9:30 Candle

```python
WAIT until 3:30 PM CEST (market open)
WAIT until 3:35 PM CEST (first 5-min candle closes)

SET candle_high = high of 3:30–3:35 candle
SET candle_low  = low of 3:30–3:35 candle

SEND Telegram: "9:30 candle captured
  High: {candle_high}
  Low:  {candle_low}
  Range: {candle_high - candle_low}"

IF range < minimum_range:
  SEND Telegram: "Range too small — skipping today"
  EXIT
```

**Minimum range thresholds:**
- PLTR: $0.50
- VUSA: €0.30
- SPY: $0.30

---

## PHASE 3: Watch for Breakout

```python
EVERY 1 minute POLL latest 1-min candle:

  IF trade_taken = True  → SKIP
  IF time > 5:00 PM CEST → CLOSE window → EXIT

  IF candle closes ABOVE candle_high:
    SET breakout_direction = LONG
    SET breakout_confirmed = True
    SEND Telegram: "📈 Breakout UP above {candle_high}
      Watching for retest..."

  ELSE IF candle closes BELOW candle_low:
    SET breakout_direction = SHORT
    SET breakout_confirmed = True
    SEND Telegram: "📉 Breakout DOWN below {candle_low}
      Watching for retest..."
```

---

## PHASE 4: Watch for Retest

```python
IF breakout_confirmed = True:

  EVERY 1 minute POLL latest 1-min candle:

    IF time > 5:00 PM CEST → EXIT, no trade

    ── LONG SCENARIO ──────────────────────────────────

    IF breakout_direction = LONG:

      IF candle LOW touches candle_high:        # price returned to level

        IF candle CLOSES ABOVE candle_high:     # ✅ successful retest
          SET retest_confirmed = True
          SET entry      = candle_high
          SET stop_loss  = candle_low
          SET take_profit = entry + 2 × (entry - stop_loss)
          → GO TO PHASE 5

        ELSE IF candle CLOSES BELOW candle_high: # ❌ failed retest
          SEND Telegram: "⚠️ Failed retest — skipping today"
          EXIT

    ── SHORT SCENARIO ─────────────────────────────────

    IF breakout_direction = SHORT:

      IF candle HIGH touches candle_low:        # price returned to level

        IF candle CLOSES BELOW candle_low:      # ✅ successful retest
          SET retest_confirmed = True
          SET entry      = candle_low
          SET stop_loss  = candle_high
          SET take_profit = entry - 2 × (stop_loss - entry)
          → GO TO PHASE 5

        ELSE IF candle CLOSES ABOVE candle_low: # ❌ failed retest
          SEND Telegram: "⚠️ Failed retest — skipping today"
          EXIT
```

---

## PHASE 5: Place Order

```python
IF retest_confirmed = True AND trade_taken = False:

  ── POSITION SIZING ────────────────────────────────

  risk_per_share = ABS(entry - stop_loss)
  max_risk       = account_balance × 0.02     # risk max 2% per trade
  shares         = FLOOR(max_risk / risk_per_share)

  ── RVOL FILTER ────────────────────────────────────

  IF current_volume < 1.5 × average_volume:
    SEND Telegram: "⚠️ Low volume — skipping"
    EXIT

  ── PLACE BRACKET ORDER ON IBKR ────────────────────

  PLACE bracket order:
    Side:        LONG or SHORT
    Type:        Limit
    Entry:       {entry}
    Stop Loss:   {stop_loss}
    Take Profit: {take_profit}
    Quantity:    {shares}

  SET trade_taken = True

  SEND Telegram: "✅ Order placed
    Side:     {direction}
    Entry:    {entry}
    SL:       {stop_loss}
    TP:       {take_profit}
    Shares:   {shares}
    Max risk: ${max_risk}"
```

---

## PHASE 6: Monitor Trade

```python
EVERY 1 minute POLL position status:

  ── TP HIT ─────────────────────────────────────────

  IF position closed by TP:
    SEND Telegram: "🎯 TP Hit!
      Profit:  +${profit}
      Balance: ${new_balance}"
    LOG trade to journal
    EXIT

  ── SL HIT ─────────────────────────────────────────

  IF position closed by SL:
    SEND Telegram: "❌ SL Hit
      Loss:    -${loss}
      Balance: ${new_balance}"
    LOG trade to journal
    EXIT

  ── PRICE RE-ENTERS RANGE ──────────────────────────

  IF price re-enters range after entry:
    CLOSE position immediately
    SEND Telegram: "🚪 Price re-entered range
      Exiting early at ${current_price}"
    LOG trade to journal
    EXIT

  ── WINDOW CLOSES ──────────────────────────────────

  IF time > 5:00 PM CEST AND position still open:
    CLOSE position at market
    SEND Telegram: "⏰ Window closed
      Closing at market: ${current_price}"
    LOG trade to journal
    EXIT
```

---

## PHASE 7: End of Day Log

```python
LOG to journal:
  - Date
  - Symbol
  - Direction         (LONG / SHORT)
  - Entry price
  - Exit price
  - Shares
  - Profit / Loss
  - Win or Loss
  - Exit reason       (TP / SL / Time / Failed retest / No setup)

RESET all variables for next day
```

---

## Filters — Skip Trade If Any Are True

| Condition | Action |
|-----------|--------|
| Range too small (< $0.50 PLTR / < €0.30 VUSA) | ❌ Skip today |
| Failed retest (candle closes wrong side of level) | ❌ Skip today |
| RVOL < 1.5 at breakout | ⚠️ Skip or reduce size |
| Breakout happens after 4:30 PM CEST | ❌ Too late for 2R |
| Already took 1 trade today on this instrument | ❌ Max 1 trade per session |
| Price re-enters range after breakout | 🚪 Exit immediately |

---

## Retest Scenarios — Decision Table

| Scenario | What Happened | Bot Action |
|----------|--------------|------------|
| Candle closes above/below level | Breakout confirmed | Watch for retest |
| Retest candle touches level + closes above (long) | ✅ Successful retest | Enter long |
| Retest candle touches level + closes below (short) | ✅ Successful retest | Enter short |
| Retest candle closes back inside range | ❌ Failed retest | Skip today |
| Price never retests within window | ⏭️ No setup | Wait tomorrow |
| Price re-enters range after entry | ⚠️ Invalid setup | Exit immediately |
| Low volume at breakout (RVOL < 1.5) | ⚠️ Weak signal | Skip or reduce size |
| Breakout after window closes | ❌ Outside window | Ignore |
| SL hit | ❌ Loss | Close, log, Telegram |
| TP hit | ✅ Profit | Close, log, Telegram |

---

## Telegram Notification Reference

| Event | Message |
|-------|---------|
| Session starting | 🔔 EU/US session starting — watching {symbol} candle |
| Candle captured | 📊 9:30 candle captured. High: {x} Low: {y} Range: {z} |
| Range too small | 😴 Range too small — skipping today |
| Breakout up | 📈 {symbol} broke above {level} — watching for retest |
| Breakout down | 📉 {symbol} broke below {level} — watching for retest |
| Failed retest | ⚠️ Failed retest detected — skipping today |
| Order placed | ✅ {direction} {symbol} at {entry}, SL {sl}, TP {tp} |
| TP hit | 🎯 TP hit! +${profit} profit |
| SL hit | ❌ SL hit. -${loss} loss |
| Time exit | ⏰ Window closed — exiting at market |
| No setup | 😴 No clean setup today — window closed |

---

## Scheduler (Windows Task Scheduler)

| Time (CEST) | Action |
|-------------|--------|
| 8:50 AM | Start EU session script |
| 9:05 AM | EU candle captured — watch for breakout |
| 10:30 AM | EU window closes — cancel pending orders |
| 3:20 PM | Start US session script |
| 3:35 PM | US candle captured — watch for breakout |
| 5:00 PM | US window closes — cancel pending orders |

---

## Stack

- **Language:** Python
- **Broker API:** IBKR via `ib_async` (already in Signal Mesh)
- **Notifications:** Telegram bot (already exists)
- **Scheduler:** Windows Task Scheduler
- **Mode:** Paper trading flag → switch to live when ready

---

## Phase Rollout

### Phase 1 — Semi-Automated (Build First)
Bot detects breakout → sends Telegram alert → you manually confirm retest and place order.

### Phase 2 — Fully Automated (After 4 Weeks of Data)
Bot detects retest using break → retest → re-break confirmation logic and places order automatically.

---

## Backtest Results (Scarface Trades — 1,308 trades)

| Metric | Result |
|--------|--------|
| Total trades | 1,308 |
| Win rate | ~59% |
| Profit factor | 4.15 |
| Risk/Reward | 2R |
| Biggest winner | $23,674 |
| Biggest loser | $11,554 |

> Expected value per trade = (0.59 × 2R) − (0.41 × 1R) = **+0.77R per trade**

---

*Last updated: June 2026 | Strategy: Scarface Trades 9:30 AM Candle | Adapted for EU/US dual session*
