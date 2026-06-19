#!/usr/bin/env python3
"""Portfolio analyzer v1. Reads holdings.csv, fetches prices, reports
P&L, concentration, and tax-loss harvesting candidates."""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).parent
HOLDINGS = ROOT / "holdings.csv"
CACHE_DIR = ROOT / "cache"
CONCENTRATION_FLAG = 0.10  # 10% — per CLAUDE.md house rule
TLH_MIN_LOSS = 500.0       # don't bother flagging tiny losses
LTCG_DAYS = 365


def load_holdings() -> pd.DataFrame:
    df = pd.read_csv(HOLDINGS, parse_dates=["acquired"])
    df["cost_basis"] = df["qty"] * df["cost_basis_per_share"]
    return df


def fetch_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch latest close. Cache per-day to avoid hammering yfinance."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"prices-{date.today().isoformat()}.json"
    if cache_file.exists():
        cached = json.loads(cache_file.read_text())
        if all(s in cached for s in symbols):
            return {s: cached[s] for s in symbols}
    else:
        cached = {}

    tickers = yf.Tickers(" ".join(symbols))
    out = dict(cached)
    for s in symbols:
        hist = tickers.tickers[s].history(period="5d")
        close = hist["Close"].dropna() if not hist.empty else hist.get("Close", pd.Series(dtype=float))
        if close.empty:
            raise RuntimeError(f"no price data for {s}")
        out[s] = float(close.iloc[-1])
    cache_file.write_text(json.dumps(out, indent=2))
    return {s: out[s] for s in symbols}


def enrich(df: pd.DataFrame, prices: dict[str, float], today: date) -> pd.DataFrame:
    df = df.copy()
    df["price"] = df["symbol"].map(prices)
    df["market_value"] = df["qty"] * df["price"]
    df["unrealized_pl"] = df["market_value"] - df["cost_basis"]
    df["unrealized_pct"] = df["unrealized_pl"] / df["cost_basis"]
    df["holding_days"] = (pd.Timestamp(today) - df["acquired"]).dt.days
    df["term"] = df["holding_days"].apply(lambda d: "LT" if d >= LTCG_DAYS else "ST")
    return df


def fmt_money(x: float) -> str:
    sign = "-" if x < 0 else " "
    return f"{sign}${abs(x):>12,.2f}"


def fmt_pct(x: float) -> str:
    return f"{x*100:>+7.2f}%"


def section(title: str) -> None:
    print(f"\n{title}")
    print("=" * len(title))


def report_totals(df: pd.DataFrame) -> None:
    section("Portfolio Totals")
    total_mv = df["market_value"].sum()
    total_cb = df["cost_basis"].sum()
    total_pl = df["unrealized_pl"].sum()
    print(f"  Market value:    {fmt_money(total_mv)}")
    print(f"  Cost basis:      {fmt_money(total_cb)}")
    print(f"  Unrealized P&L:  {fmt_money(total_pl)}  ({fmt_pct(total_pl/total_cb)})")
    print(f"  Lots:            {len(df)}  across {df['symbol'].nunique()} symbols")


def report_concentration(df: pd.DataFrame, by: str, label: str) -> None:
    section(f"Concentration by {label}")
    total = df["market_value"].sum()
    grouped = (df.groupby(by)["market_value"].sum()
                 .sort_values(ascending=False))
    for key, mv in grouped.items():
        pct = mv / total
        flag = "  <-- >10%" if pct > CONCENTRATION_FLAG else ""
        print(f"  {str(key):<22} {fmt_money(mv)}  {fmt_pct(pct)}{flag}")


def report_positions(df: pd.DataFrame) -> None:
    section("By Position (aggregated across lots)")
    g = df.groupby("symbol").agg(
        qty=("qty", "sum"),
        cost_basis=("cost_basis", "sum"),
        market_value=("market_value", "sum"),
        unrealized_pl=("unrealized_pl", "sum"),
    ).sort_values("market_value", ascending=False)
    g["pct"] = g["unrealized_pl"] / g["cost_basis"]
    print(f"  {'sym':<6} {'qty':>8} {'mkt value':>15} {'unreal P&L':>15} {'%':>9}")
    for sym, row in g.iterrows():
        print(f"  {sym:<6} {row['qty']:>8.2f} "
              f"{fmt_money(row['market_value'])} "
              f"{fmt_money(row['unrealized_pl'])} "
              f"{fmt_pct(row['pct'])}")


def report_tlh(df: pd.DataFrame) -> None:
    section("Tax-Loss Harvesting Candidates (taxable accounts only)")
    taxable = df[(df["account_type"] == "taxable") &
                 (df["unrealized_pl"] <= -TLH_MIN_LOSS)]
    if taxable.empty:
        print("  None at current prices.")
        return
    print(f"  {'lot_id':<24} {'sym':<6} {'term':<4} {'loss':>15} {'%':>9}")
    total_loss = 0.0
    for _, row in taxable.sort_values("unrealized_pl").iterrows():
        print(f"  {row['lot_id']:<24} {row['symbol']:<6} {row['term']:<4} "
              f"{fmt_money(row['unrealized_pl'])} "
              f"{fmt_pct(row['unrealized_pct'])}")
        total_loss += row["unrealized_pl"]
    print(f"  {'TOTAL HARVESTABLE':<35} {fmt_money(total_loss)}")
    print("  Note: check wash-sale window (30d) before/after sale.")
    print("  Note: only realize if you have offsetting gains or $3k ordinary use.")


def main() -> int:
    df = load_holdings()
    symbols = sorted(df["symbol"].unique().tolist())
    try:
        prices = fetch_prices(symbols)
    except Exception as e:
        print(f"price fetch failed: {e}", file=sys.stderr)
        return 1
    df = enrich(df, prices, date.today())

    print(f"As of {date.today().isoformat()}  (prices: latest close)")
    report_totals(df)
    report_positions(df)
    report_concentration(df, "symbol", "Symbol")
    report_concentration(df, "asset_class", "Asset Class")
    report_concentration(df, "region", "Region")
    report_tlh(df)
    return 0


if __name__ == "__main__":
    sys.exit(main())
