"""
test_account_summary.py — Verify the bot can read live account data from IBKR.

This test MUST pass before trusting any sizing logic in the live engine.  If
get_account_summary() silently returns zero or empty, every order placed that
session could be incorrectly sized against a garbage number — the same silent-
failure pattern as the net==0.0000 mesh bug.

Steps
─────
  1  Connect to IBKR paper account
  2  Call get_account_summary() immediately after connect (tests retry logic)
  3  Assert NLV > 0 and AvailableFunds > 0 (zero = broken)
  4  Print all three figures so you can eyeball them against TWS UI

Usage
─────
  python tst/test_account_summary.py

Pass condition
──────────────
  net_liquidation > 0  AND  available_funds > 0
  Values printed should match what TWS shows for the paper account.
"""
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent)); import setup_paths  # noqa: E402
import sys

import config
import ibkr_connector

_PASS = "✅ PASS"
_FAIL = "❌ FAIL"


def run_test() -> bool:
    results: dict[str, str] = {}
    account: dict = {}

    # ── Step 1: connect ───────────────────────────────────────────────────────
    print("\n" + "─" * 60)
    print("  STEP 1: IBKR connection")
    print("─" * 60)
    try:
        ib = ibkr_connector.connect()
        accounts = ib.managedAccounts()
        print(f"  Connected  ✓  |  accounts: {accounts}")
        results["1_connection"] = _PASS
    except Exception as e:
        print(f"  Connection FAILED: {e}")
        results["1_connection"] = _FAIL
        _print_summary(results)
        return False

    # ── Step 2: read account summary (immediate call to exercise retry loop) ─
    print("\n" + "─" * 60)
    print("  STEP 2: get_account_summary() — immediately after connect()")
    print("─" * 60)
    try:
        account = ibkr_connector.get_account_summary(ib)
        print(f"  NetLiquidation : ${account['net_liquidation']:,.2f}")
        print(f"  AvailableFunds : ${account['available_funds']:,.2f}")
        print(f"  BuyingPower    : ${account['buying_power']:,.2f}")
        results["2_account_summary"] = _PASS
    except Exception as e:
        print(f"  get_account_summary() FAILED: {e}")
        results["2_account_summary"] = _FAIL
        ib.disconnect()
        _print_summary(results)
        return False

    # ── Step 3: assert values are non-zero ────────────────────────────────────
    print("\n" + "─" * 60)
    print("  STEP 3: Assert NLV > 0 and AvailableFunds > 0")
    print("─" * 60)
    nlv   = account.get("net_liquidation", 0)
    avail = account.get("available_funds", 0)

    if nlv <= 0:
        print(f"  FAIL: NetLiquidation={nlv} — zero or negative value received.")
        print("        This means the bot would size orders off a garbage number.")
        results["3_nonzero_values"] = _FAIL
    elif avail <= 0:
        print(f"  FAIL: AvailableFunds={avail} — zero or negative value received.")
        print("        Account may have no free funds, or the field wasn't populated.")
        results["3_nonzero_values"] = _FAIL
    else:
        print(f"  NLV ${nlv:,.2f}  AvailableFunds ${avail:,.2f}  — both positive  ✓")
        results["3_nonzero_values"] = _PASS

    # ── Step 4: risk_usd sanity check ─────────────────────────────────────────
    print("\n" + "─" * 60)
    print("  STEP 4: risk_usd derived from live account vs config fallback")
    print("─" * 60)
    from session_runtime import _risk_usd, _max_notional
    live_risk    = _risk_usd(account)
    config_risk  = _risk_usd(None)
    live_max_n   = _max_notional(account)
    config_max_n = _max_notional(None)
    print(f"  Live account   risk_usd = ${live_risk:.2f}  max_notional = ${live_max_n:,.2f}")
    print(f"  Config fallback risk_usd = ${config_risk:.2f}  max_notional = ${config_max_n:,.2f}")
    if live_risk > 0:
        results["4_risk_usd"] = _PASS
    else:
        print("  FAIL: live risk_usd is zero — sizing would produce qty=0 for every trade.")
        results["4_risk_usd"] = _FAIL

    ib.disconnect()
    print("\n  Disconnected.")
    return _print_summary(results)


def _print_summary(results: dict[str, str]) -> bool:
    print("\n" + "=" * 60)
    print("  TEST SUMMARY")
    print("=" * 60)
    all_pass = True
    for step, result in results.items():
        print(f"  {result}  {step}")
        if result != _PASS:
            all_pass = False
    print()
    if all_pass:
        print("  Overall: ✅  ALL STEPS PASSED")
    else:
        print("  Overall: ❌  SOME STEPS FAILED — do NOT run the live engine until fixed")
    print("=" * 60 + "\n")
    return all_pass


if __name__ == "__main__":
    ok = run_test()
    sys.exit(0 if ok else 1)
