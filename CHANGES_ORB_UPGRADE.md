# ORB Architecture Upgrade — Change List

> Do these changes to bring `run_ticker` (ORB) in line with `run_ticker_ib` and
> `run_ticker_vwap` — consistent naming, window end, co-location, and no duplicate main().

---

## lib/orb_strategy.py

### 1. Rename `run_ticker` → `run_ticker_orb` (line 164)
Consistent with `run_ticker_ib` and `run_ticker_vwap` in `live_engine.py`.

```python
# Before
def run_ticker(pick, ib, state, state_lock):

# After
def run_ticker_orb(pick, ib, state, state_lock):
```

---

### 2. Add `US_ORB_WINDOW_END` constant (after line 39)
ORB currently polls until 22:00 NL (16:00 ET). Give it its own cutoff like IB and VWAP have.

```python
US_ORB_WINDOW_END = "19:00"   # 13:00 ET — stop looking for ORB setup
```

---

### 3. Add `window_end` variable inside `run_ticker_orb` (near line 176)
Add just below `session_end`:

```python
session_end = _today_at(US_SESSION_END)
window_end  = _today_at(US_ORB_WINDOW_END)   # ← add this line
```

---

### 4. Update the polling loop guard (line 234)
Apply `window_end` so ORB stops scanning at 19:00 NL, not 22:00.

```python
# Before
while datetime.now(NL) < session_end:

# After
while datetime.now(NL) < window_end:
```

> `_monitor_bracket` still uses `session_end` for the open position — that's correct.
> The window only applies to the *entry scan* phase.

---

### 5. Replace end-of-loop message (line 300)
Mirror the IB/VWAP "standing aside" style.

```python
# Before
send_message(f"😴 {ticker} — no clean ORB setup today, window closed.")

# After
send_message(f"😴 {ticker} ORB — no clean setup by {US_ORB_WINDOW_END} NL, standing aside.")
```

---

### 6. Remove standalone `main()` (lines 377–438)
This duplicates `live_engine.py`'s orchestration and was only needed before the
dispatcher existed. Replace with a shim or remove entirely.

```python
# Option A — thin shim (keeps `python lib/orb_strategy.py` working)
if __name__ == "__main__":
    import live_engine
    live_engine.main()

# Option B — remove the if __name__ block entirely
```

---

## bin/live_engine.py

### 7. Update import to use the new function name (lines 39–45)

```python
# Before
from orb_strategy import (
    ...
    run_ticker as run_ticker_orb,
)

# After
from orb_strategy import (
    ...
    run_ticker_orb,
)
```

---

### 8. Add `US_ORB_WINDOW_END` to timing constants block (after line 49)
All three strategy windows now visible side-by-side:

```python
US_ORB_WINDOW_END  = "19:00"   # 13:00 ET — stop looking for ORB breakout
US_IB_RANGE_END    = "16:30"   # (already there)
US_IB_WINDOW_END   = "17:00"   # (already there)
US_VWAP_WINDOW_END = "20:00"   # (already there)
```

> You can either import it from `orb_strategy` or re-declare it here — re-declaring
> is cleaner since all timing constants live in one place in this file.

---

### 9. Remove the `if runner is run_ticker_orb` special-case in `main()` (lines 396–409)

```python
# Before — two branches because old signature didn't match
if runner is run_ticker_orb:
    t = threading.Thread(target=run_ticker_orb, args=(pick, ib, state, state_lock), ...)
else:
    t = threading.Thread(target=runner, args=(pick, ib, state, state_lock), ...)

# After — single branch, all runners share the same signature
t = threading.Thread(
    target=runner,
    args=(pick, ib, state, state_lock),
    name=f"{strategy}-{pick['ticker']}",
    daemon=True,
)
```

---

## tst/test_threading.py

### 10. No changes needed ✓
- Line 167: `from orb_strategy import _eod_safety_flatten` — helper stays in `orb_strategy.py`
- Line 186: `patch("orb_strategy.ibkr_connector.close_position_at_market", ...)` — path still correct

---

## Verification

Run these after making the changes:

```bash
# 1. Offline threading + EOD flatten (no IBKR needed)
python tst/test_threading.py

# 2. Confirm live_engine imports without errors
python -c "import sys; sys.path.insert(0,'.'); import setup_paths; import live_engine"

# 3. Full integration test (requires TWS/Gateway on paper account)
python tst/test_integration.py --skip-ai
```
