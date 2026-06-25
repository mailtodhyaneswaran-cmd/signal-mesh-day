"""
test_connection.py — IBKR paper account verification tool.

Tests: connect, qualify contract, fetch bars, place + inspect bracket order.
Adapted from candle-scalping-bot/test_connection.py.

Usage:
  python test_connection.py                         # connection check only
  python test_connection.py --buy 1 NVDA            # preview + place BUY bracket
  python test_connection.py --sell 1 NVDA           # preview + place SELL bracket
  python test_connection.py --status                 # show open orders + positions
  python test_connection.py --eurusd                 # print live EUR/USD rate
"""
import argparse

import config
import ibkr_connector


def run_connection_test(ib, ticker: str, exchange: str, currency: str) -> None:
    contract = ibkr_connector.get_contract(ticker, exchange, currency)
    ib.qualifyContracts(contract)
    print(f"Contract qualified: {contract}")

    bars = ib.reqHistoricalData(
        contract, endDateTime="", durationStr="600 S",
        barSizeSetting="5 mins", whatToShow="TRADES",
        useRTH=False, formatDate=1,
    )
    if bars:
        b = bars[-1]
        print(f"Last 5-min bar — {b.date}  O:{b.open} H:{b.high} L:{b.low} C:{b.close} V:{b.volume}")
    else:
        print("No bars returned (market may be closed).")


def _get_market_price(ib, contract) -> float | None:
    tickers = ib.reqTickers(contract)
    if tickers and tickers[0].last and tickers[0].last > 0:
        return tickers[0].last
    bars = ib.reqHistoricalData(
        contract, endDateTime="", durationStr="300 S",
        barSizeSetting="1 min", whatToShow="TRADES",
        useRTH=False, formatDate=1,
    )
    return bars[-1].close if bars else None


def run_bracket_order(ib, ticker: str, exchange: str, currency: str,
                      action: str, qty: int, risk: float, offset: float) -> None:
    contract = ibkr_connector.get_contract(ticker, exchange, currency)
    ib.qualifyContracts(contract)
    ib.reqMarketDataType(3)   # delayed data — free
    price = _get_market_price(ib, contract)
    if price is None:
        print("Could not get market price.")
        return

    rr = config.INTRADAY_PARAMS.tp_r_multiple
    if action == "BUY":
        entry       = round(price - offset, 2)
        stop_loss   = round(entry - risk, 2)
        take_profit = round(entry + rr * risk, 2)
    else:
        entry       = round(price + offset, 2)
        stop_loss   = round(entry + risk, 2)
        take_profit = round(entry - rr * risk, 2)

    print(f"\nBracket preview — {action} {qty}x {ticker}")
    print(f"  Market price:  {price:.2f} {currency}")
    print(f"  Entry (limit): {entry:.2f}  (offset {offset:.2f} from market)")
    print(f"  Stop Loss:     {stop_loss:.2f}  (risk {risk:.2f}/share × {qty} = {risk*qty:.2f})")
    print(f"  Take Profit:   {take_profit:.2f}  ({rr}R = {rr*risk*qty:.2f})")

    confirm = input("\nType 'yes' to submit: ").strip().lower()
    if confirm != "yes":
        print("Cancelled.")
        return

    trades = ibkr_connector.place_bracket_order(ib, contract, action, qty,
                                                entry, take_profit, stop_loss)
    print(f"\nSubmitted {len(trades)} legs:")
    for t in trades:
        print(f"  orderId={t.order.orderId}  {t.order.action}  "
              f"{t.order.orderType}  qty={t.order.totalQuantity}")

    ib.sleep(2)
    run_status(ib)


def run_status(ib) -> None:
    orders = ib.reqAllOpenOrders()
    ib.sleep(2)
    if orders:
        print(f"\nOpen orders ({len(orders)}):")
        for o in orders:
            print(f"  {o.order.orderId:<6} {o.order.action:<4} {o.order.totalQuantity} "
                  f"{o.contract.symbol:<8} {o.order.orderType:<10} "
                  f"lmt={getattr(o.order,'lmtPrice','-')}  "
                  f"aux={getattr(o.order,'auxPrice','-')}  "
                  f"status={o.orderStatus.status}")
    else:
        print("\nNo open orders.")

    positions = ib.positions()
    if positions:
        print(f"\nPositions ({len(positions)}):")
        for p in positions:
            print(f"  {p.contract.symbol:<8} qty={p.position}  avg={p.avgCost:.2f}")
    else:
        print("No open positions.")


def main() -> None:
    parser = argparse.ArgumentParser(description="IBKR paper account test tool")
    parser.add_argument("ticker",   nargs="?", default="NVDA")
    parser.add_argument("--exchange", default="SMART")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--buy",  type=int, metavar="QTY")
    parser.add_argument("--sell", type=int, metavar="QTY")
    parser.add_argument("--risk",   type=float, default=0.50)
    parser.add_argument("--offset", type=float, default=5.00)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--eurusd", action="store_true")
    args = parser.parse_args()

    if args.eurusd:
        rate = ibkr_connector.get_eurusd_rate()
        print(f"EUR/USD spot: {rate:.4f}")
        return

    print(f"Connecting to {config.IBKR_HOST}:{config.IBKR_PORT} (clientId={config.IBKR_CLIENT_ID}) ...")
    ib = ibkr_connector.connect()
    print(f"Connected: {ib.isConnected()}  accounts: {ib.managedAccounts()}")

    if args.status:
        run_status(ib)
    elif args.buy:
        run_bracket_order(ib, args.ticker, args.exchange, args.currency,
                          "BUY", args.buy, args.risk, args.offset)
    elif args.sell:
        run_bracket_order(ib, args.ticker, args.exchange, args.currency,
                          "SELL", args.sell, args.risk, args.offset)
    else:
        run_connection_test(ib, args.ticker, args.exchange, args.currency)

    ib.disconnect()
    print("\nDisconnected cleanly.")


if __name__ == "__main__":
    main()
